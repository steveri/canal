from hwtypes import BitVector
from gemstone.common.dummy_core_magma import DummyCore
from gemstone.common.testers import BasicTester
from canal.interconnect import *
import tempfile
import fault.random
from canal.util import create_uniform_interconnect, SwitchBoxType
from canal.global_signal import apply_global_fanout_wiring, \
    apply_global_meso_wiring
import pytest
import enum


def assert_tile_coordinate(tile: Tile, x: int, y: int):
    assert tile.x == x and tile.y == y
    for sb in tile.switchbox.get_all_sbs():
        assert_coordinate(sb, x, y)
    for _, node in tile.ports.items():
        assert_coordinate(node, x, y)
    for _, node in tile.switchbox.registers.items():
        assert_coordinate(node, x, y)


def assert_coordinate(node: Node, x: int, y: int):
    assert node.x == x and node.y == y


# add enum to make pytest more readable
@enum.unique
class GlobalSignalWiring(enum.Enum):
    Fanout = enum.auto()
    Meso = enum.auto()


@pytest.mark.parametrize("num_tracks", [2, 4])
@pytest.mark.parametrize("chip_size", [2, 4])
@pytest.mark.parametrize("reg_mode", [True, False])
@pytest.mark.parametrize("wiring", [GlobalSignalWiring.Fanout,
                                    GlobalSignalWiring.Meso])
def test_interconnect(num_tracks: int, chip_size: int,
                      reg_mode: bool,
                      wiring: GlobalSignalWiring):
    addr_width = 8
    data_width = 32
    bit_widths = [1, 16]

    tile_id_width = 16

    track_length = 1

    # creates all the cores here
    # we don't want duplicated cores when snapping into different interconnect
    # graphs
    cores = {}
    for x in range(chip_size):
        for y in range(chip_size):
            cores[(x, y)] = DummyCore()

    def create_core(xx: int, yy: int):
        return cores[(xx, yy)]

    in_conn = []
    out_conn = []
    for side in SwitchBoxSide:
        in_conn.append((side, SwitchBoxIO.SB_IN))
        out_conn.append((side, SwitchBoxIO.SB_OUT))

    pipeline_regs = []
    for track in range(num_tracks):
        for side in SwitchBoxSide:
            pipeline_regs.append((track, side))
    # if reg mode is off, reset to empty
    if not reg_mode:
        pipeline_regs = []
    ics = {}
    for bit_width in bit_widths:
        ic = create_uniform_interconnect(chip_size, chip_size, bit_width,
                                         create_core,
                                         {f"data_in_{bit_width}b": in_conn,
                                          f"data_out_{bit_width}b": out_conn},
                                         {track_length: num_tracks},
                                         SwitchBoxType.Disjoint,
                                         pipeline_regs)
        ics[bit_width] = ic

    interconnect = Interconnect(ics, addr_width, data_width, tile_id_width,
                                lift_ports=True)

    # finalize the design
    interconnect.finalize()

    # wiring
    if wiring == GlobalSignalWiring.Fanout:
        apply_global_fanout_wiring(interconnect)
    else:
        assert wiring == GlobalSignalWiring.Meso
        apply_global_meso_wiring(interconnect)

    # assert tile coordinates
    for (x, y), tile_circuit in interconnect.tile_circuits.items():
        for _, tile in tile_circuit.tiles.items():
            assert_tile_coordinate(tile, x, y)

    circuit = interconnect.circuit()
    tester = BasicTester(circuit, circuit.clk, circuit.reset)
    config_data = []
    test_data = []

    # we have a 2x2 (for instance) chip as follows
    # |-------|
    # | A | B |
    # |---|---|
    # | C | D |
    # ---------
    # we need to test all the line-tile routes. that is, per row and per column
    # A <-> B, A <-> C, B <-> D, C <-> D
    # TODO: add core input/output as well

    vertical_conns = [[SwitchBoxSide.NORTH, SwitchBoxIO.SB_IN,
                       SwitchBoxSide.SOUTH, SwitchBoxIO.SB_OUT,
                       0, chip_size - 1],
                      [SwitchBoxSide.SOUTH, SwitchBoxIO.SB_IN,
                       SwitchBoxSide.NORTH, SwitchBoxIO.SB_OUT,
                       chip_size - 1, 0]]

    horizontal_conns = [[SwitchBoxSide.WEST, SwitchBoxIO.SB_IN,
                         SwitchBoxSide.EAST, SwitchBoxIO.SB_OUT,
                         0, chip_size - 1],
                        [SwitchBoxSide.EAST, SwitchBoxIO.SB_IN,
                         SwitchBoxSide.WEST, SwitchBoxIO.SB_OUT,
                         chip_size - 1, 0]]

    for bit_width in bit_widths:
        for track in range(num_tracks):
            # vertical
            for x in range(chip_size):
                for side_io in vertical_conns:
                    from_side, from_io, to_side, to_io, start, end = side_io
                    src_node = None
                    dst_node = None
                    config_entry = []
                    for y in range(chip_size):
                        tile_circuit = interconnect.tile_circuits[(x, y)]
                        tile = tile_circuit.tiles[bit_width]
                        pre_node = tile.get_sb(from_side, track, from_io)
                        tile_circuit = interconnect.tile_circuits[(x, y)]
                        tile = tile_circuit.tiles[bit_width]
                        next_node = tile.get_sb(to_side, track, to_io)
                        if y == start:
                            src_node = pre_node
                        if y == end:
                            dst_node = next_node

                        entry = \
                            interconnect.get_node_bitstream_config(pre_node,
                                                                   next_node)

                        config_entry.append(entry)
                    assert src_node is not None and dst_node is not None
                    config_data.append(config_entry)
                    value = fault.random.random_bv(bit_width)
                    src_name = create_name(str(src_node)) + \
                        f"_X{src_node.x}_Y{src_node.y}"
                    dst_name = create_name(str(dst_node)) + \
                        f"_X{dst_node.x}_Y{dst_node.y}"
                    test_data.append((circuit.interface[src_name],
                                      circuit.interface[dst_name],
                                      value))

            # horizontal connections
            for y in range(chip_size):
                for side_io in horizontal_conns:
                    from_side, from_io, to_side, to_io, start, end = side_io
                    src_node = None
                    dst_node = None
                    config_entry = []
                    for x in range(chip_size):
                        tile_circuit = interconnect.tile_circuits[(x, y)]
                        tile = tile_circuit.tiles[bit_width]
                        pre_node = tile.get_sb(from_side, track, from_io)
                        tile_circuit = interconnect.tile_circuits[(x, y)]
                        tile = tile_circuit.tiles[bit_width]
                        next_node = tile.get_sb(to_side, track, to_io)
                        if x == start:
                            src_node = pre_node
                        if x == end:
                            dst_node = next_node

                        entry = \
                            interconnect.get_node_bitstream_config(pre_node,
                                                                   next_node)

                        config_entry.append(entry)
                    assert src_node is not None and dst_node is not None
                    config_data.append(config_entry)
                    value = fault.random.random_bv(bit_width)
                    src_name = create_name(str(src_node)) + \
                        f"_X{src_node.x}_Y{src_node.y}"
                    dst_name = create_name(str(dst_node)) + \
                        f"_X{dst_node.x}_Y{dst_node.y}"
                    test_data.append((circuit.interface[src_name],
                                      circuit.interface[dst_name],
                                      value))

    # the actual test
    assert len(config_data) == len(test_data)
    # NOTE:
    # we don't test the configuration read here
    for i in range(len(config_data)):
        tester.reset()
        input_port, output_port, value = test_data[i]
        for addr, index in config_data[i]:
            tester.configure(BitVector(addr, data_width), index)
            tester.configure(BitVector(addr, data_width), index + 1, False)
            tester.config_read(addr)
            tester.eval()
            tester.expect(circuit.read_config_data, index)

        tester.poke(input_port, value)
        tester.eval()
        tester.expect(output_port, value)

    with tempfile.TemporaryDirectory() as tempdir:
        tester.compile_and_run(target="verilator",
                               magma_output="coreir-verilog",
                               directory=tempdir,
                               flags=["-Wno-fatal"])


def test_dump_pnr():
    num_tracks = 2
    addr_width = 8
    data_width = 32
    bit_widths = [1, 16]

    tile_id_width = 16

    chip_size = 2
    track_length = 1

    # creates all the cores here
    # we don't want duplicated cores when snapping into different interconnect
    # graphs
    cores = {}
    for x in range(chip_size):
        for y in range(chip_size):
            cores[(x, y)] = DummyCore()

    def create_core(xx: int, yy: int):
        return cores[(xx, yy)]

    in_conn = []
    out_conn = []
    for side in SwitchBoxSide:
        in_conn.append((side, SwitchBoxIO.SB_IN))
        out_conn.append((side, SwitchBoxIO.SB_OUT))

    pipeline_regs = []
    for track in range(num_tracks):
        for side in SwitchBoxSide:
            pipeline_regs.append((track, side))

    ics = {}
    for bit_width in bit_widths:
        ic = create_uniform_interconnect(chip_size, chip_size, bit_width,
                                         create_core,
                                         {f"data_in_{bit_width}b": in_conn,
                                          f"data_out_{bit_width}b": out_conn},
                                         {track_length: num_tracks},
                                         SwitchBoxType.Disjoint,
                                         pipeline_reg=pipeline_regs)
        ics[bit_width] = ic

    interconnect = Interconnect(ics, addr_width, data_width, tile_id_width,
                                lift_ports=True)

    design_name = "test"
    with tempfile.TemporaryDirectory() as tempdir:
        interconnect.dump_pnr(tempdir, design_name)

        assert os.path.isfile(os.path.join(tempdir, f"{design_name}.info"))
        assert os.path.isfile(os.path.join(tempdir, "1.graph"))
        assert os.path.isfile(os.path.join(tempdir, "16.graph"))
        assert os.path.isfile(os.path.join(tempdir, f"{design_name}.layout"))
