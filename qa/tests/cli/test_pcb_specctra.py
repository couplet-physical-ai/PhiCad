#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""The headless Specctra round trip: `pcb export dsn` out, `pcb import-ses` back.

This is the interface an external autorouter is driven through, so what is asserted is
what a router consumes and returns: a DSN whose structure/network sections describe the
board's real copper layers and nets, and a board that gained the session's copper and was
actually written to disk.
"""

import utils
from pathlib import Path
from conftest import KiTestFixture


BOARD = "cli/basic_test/basic_test.kicad_pcb"


def test_pcb_export_dsn( kitest: KiTestFixture ):
    input_file = kitest.get_data_file_path( BOARD )
    out_dir = kitest.get_output_path( "cli/export_dsn/" )
    dsn = Path( out_dir ) / "basic_test.dsn"

    stdout, stderr, exitcode = utils.run_and_capture(
        [utils.kicad_cli(), "pcb", "export", "dsn", "-o", str( dsn ), str( input_file )] )

    assert exitcode == 0
    assert dsn.exists()
    kitest.add_attachment( dsn )

    text = dsn.read_text()

    # A router reads these three sections; a file missing any of them loads as empty.
    assert text.startswith( "(pcb " )
    assert "(structure" in text
    assert "(network" in text
    assert "(placement" in text

    # Copper layers only, under their board names, in stackup order.
    assert text.index( "(layer F.Cu" ) < text.index( "(layer B.Cu" )
    assert "(layer F.SilkS" not in text

    # Nets reach the router by name, with their pads.
    assert "(net /VCC" in text
    assert "(pins " in text


def test_pcb_import_ses( kitest: KiTestFixture ):
    input_file = kitest.get_data_file_path( BOARD )
    out_dir = Path( kitest.get_output_path( "cli/import_ses/" ) )
    ses = out_dir / "basic_test.ses"
    routed = out_dir / "basic_test-routed.kicad_pcb"

    if routed.exists():
        routed.unlink()

    source_before = input_file.read_bytes()

    # A minimal router answer: one 0.16 mm track on /VCC.  Session coordinates are in the
    # units the (resolution) line declares, not the board's.
    ses.write_text(
        "(session basic_test.ses\n"
        "  (base_design basic_test.dsn)\n"
        "  (routes\n"
        "    (resolution um 10)\n"
        "    (library_out )\n"
        "    (network_out\n"
        "      (net \"/VCC\"\n"
        "        (wire (path F.Cu 1600  1000000 -1000000  1010000 -1000000)\n"
        "          (type route))\n"
        "      )\n"
        "    )\n"
        "  )\n"
        ")\n" )

    stdout, stderr, exitcode = utils.run_and_capture(
        [utils.kicad_cli(), "pcb", "import-ses", "--ses", str( ses ),
         "-o", str( routed ), str( input_file )] )

    assert exitcode == 0
    assert routed.exists()
    kitest.add_attachment( routed )

    board = routed.read_text()

    # A session is the complete routing, not an addition: it replaces every unlocked track,
    # so this board carries the one wire the session held and none of the 26 it arrived with.
    assert board.count( "(segment" ) == 1
    assert '(net "/VCC")' in board
    assert "(width 0.16)" in board
    assert "(start 100 100)" in board and "(end 101 100)" in board

    # The source board must be left exactly as it was when -o names another file.
    assert input_file.read_bytes() == source_before


def test_pcb_import_ses_requires_session( kitest: KiTestFixture ):
    input_file = kitest.get_data_file_path( BOARD )

    stdout, stderr, exitcode = utils.run_and_capture(
        [utils.kicad_cli(), "pcb", "import-ses", str( input_file )] )

    assert exitcode != 0
