# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Register the PhiCad MCP server's contract suites with CTest.
#
# WHY THIS FILE EXISTS: the three suites in phicad/mcp/ carry 689 checks between them and
# nothing ran them.  They were reachable only by typing the venv python and the file path by
# hand, so `phicad test` / `ctest -N` did not list them and CI could not see them.  This file
# makes them first-class ctest registrations, exactly like `qa_cli` in the parent directory.
#
# They are NOT C++ and NOT pytest: each suite is a standalone python program with its own
# runner (deliberately -- the MCP venv holds fastmcp and must not grow a test framework, see
# the module docstring in phicad/mcp/test_registry.py).  So the pattern borrowed from qa_cli is
# "python interpreter + script", plus KICAD_CLI/$<TARGET_FILE:kicad-cli> and setup_qa_env().
#
# INTERPRETER: they run under the MCP venv (fastmcp, no kipy), never ${PYTHON_EXECUTABLE} --
# the system/QA python has no fastmcp, and importing kipy into this process is what the two
# venvs exist to prevent.  A checkout without that venv is normal (it is created by
# `phicad mcp setup`), so every registration here is guarded: a venv-less build still
# configures, it just registers no MCP tests.
#
# WHAT EACH ONE NEEDS:
#   qa_mcp_registry   nothing built.  Static shape of the 126 tools + the phicad-tool CLI.
#   qa_mcp_tools      a built kicad-cli (it shells out to it), --quick here to skip exports/IPC.
#   qa_mcp_handshake  a built kicad-cli, and spawns server.py as an MCP client child.

# The MCP venv is created outside the build tree (build/mcp-venv next to build/mac-dev) by
# `phicad mcp setup`; PHICAD_MCP_VENV overrides it for a worktree that borrows a sibling's.
set( PHICAD_MCP_VENV "${CMAKE_SOURCE_DIR}/build/mcp-venv"
     CACHE PATH "Virtualenv holding fastmcp for the phicad MCP suites (phicad mcp setup)" )

# The kipy/automation venv, used by the schematic and session workers the tools shell out to.
set( PHICAD_AUTOMATION_VENV "${CMAKE_SOURCE_DIR}/build/venv"
     CACHE PATH "Virtualenv holding kipy for the phicad automation workers (phicad venv)" )

set( MCP_VENV_PYTHON "${PHICAD_MCP_VENV}/bin/python" )
set( MCP_SUITE_DIR   "${CMAKE_SOURCE_DIR}/phicad/mcp" )

# Scratch root for the suites, OUTSIDE the source tree -- see the macro comment below for why
# the build dir will not do.  Keyed by a hash of the build directory so two build trees (or two
# worktrees) never share a scratch path.  Override with -DPHICAD_MCP_SCRATCH_ROOT=<dir>.
string( SHA1 _mcp_build_hash "${CMAKE_BINARY_DIR}" )
string( SUBSTRING "${_mcp_build_hash}" 0 10 _mcp_build_hash )
set( PHICAD_MCP_SCRATCH_ROOT "$ENV{TMPDIR}/phicad-qa-mcp-${_mcp_build_hash}"
     CACHE PATH "Scratch root for the phicad MCP ctest registrations (must be outside the source tree)" )
set( MCP_SCRATCH_ROOT "${PHICAD_MCP_SCRATCH_ROOT}" )

if( MCP_SCRATCH_ROOT STREQUAL "" OR MCP_SCRATCH_ROOT MATCHES "^/phicad-qa-mcp-" )
    # $TMPDIR was unset at configure time (common on CI); fall back to /tmp.
    set( MCP_SCRATCH_ROOT "/tmp/phicad-qa-mcp-${_mcp_build_hash}" )
endif()

if( NOT EXISTS "${MCP_VENV_PYTHON}" )
    message( STATUS
        "phicad MCP suites not registered: no interpreter at ${MCP_VENV_PYTHON} "
        "(create it with `phicad mcp setup`, or point -DPHICAD_MCP_VENV=<dir> at an existing one)" )
    return()
endif()

# phicad_add_mcp_test( <ctest-name> <script> <timeout-seconds> [extra script args...] )
#
# The per-test scratch directory is the point of this macro.  The suites take their scratch
# root from $TMPDIR / $PHICAD_MCP_TEST_ROOT and their export root from $PHICAD_MCP_OUT; under
# `ctest -j` every registration would otherwise share the shell's one $TMPDIR.  The suites
# also defend themselves (they mkdtemp inside that root), but a gate must not depend on the
# thing it is gating, so ctest hands each test a private root as well.  Belt and braces: if
# the in-suite isolation regresses, these tests still cannot collide with each other.
#
# The directory is created at configure time -- tempfile.mkdtemp() does not create parents,
# and a missing $TMPDIR is an ugly traceback rather than a test failure.
#
# It must also live OUTSIDE the source tree.  `project_init` deliberately refuses to create a
# git repository inside the PhiCad source repo (the guard in phicad/pylib/project_history.py:
# a board project owns its own repo and must never be nested in this one), and the house build
# dir is `build/mac-dev` *inside* the checkout -- so a scratch root under the build dir makes
# every journal stage fail with "is inside the PhiCad source repository".  Verified: it does.
# The path is keyed by a hash of the build dir so two build trees never share scratch.
macro( phicad_add_mcp_test test_name script timeout )
    set( _scratch "${MCP_SCRATCH_ROOT}/${test_name}" )
    file( MAKE_DIRECTORY "${_scratch}/tmp" "${_scratch}/out" )

    add_test( NAME ${test_name}
        COMMAND "${MCP_VENV_PYTHON}" "${MCP_SUITE_DIR}/${script}" ${ARGN}
        WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    )

    # A cmake list, joined by cmake into the semicolon-separated string the ENVIRONMENT
    # property wants.  Spelling it out one variable per line is the difference between a
    # reviewable diff and a 300-column string.
    #
    # KICAD_CLI is what qa_cli publishes and what a reader of this tree expects; the MCP
    # server reads PHICAD_KICAD_CLI (phicad/mcp/server.py:60), so set both to the same file
    # rather than relying on PHICAD_BUILD's mac-dev-shaped default bundle path.
    set( _env
        "KICAD_CLI=$<TARGET_FILE:kicad-cli>"
        "PHICAD_KICAD_CLI=$<TARGET_FILE:kicad-cli>"
        "PHICAD_REPO=${CMAKE_SOURCE_DIR}"
        "PHICAD_BUILD=${CMAKE_BINARY_DIR}"
        "PHICAD_VENV=${PHICAD_AUTOMATION_VENV}"
        "PHICAD_MCP_VENV=${PHICAD_MCP_VENV}"
        # The three scratch knobs.  TMPDIR is the fallback the suites use when
        # PHICAD_MCP_TEST_ROOT is unset; both are set so neither path can reach a shared /tmp.
        "TMPDIR=${_scratch}/tmp"
        "PHICAD_MCP_TEST_ROOT=${_scratch}/tmp"
        "PHICAD_MCP_OUT=${_scratch}/out" )

    set_tests_properties( ${test_name}
        PROPERTIES
            TIMEOUT ${timeout}
            ENVIRONMENT "${_env}"
    )

    # Adds KICAD_RUN_FROM_BUILD_DIR=1 and the build-tree PATH/KICAD_BUILD_PATHS on top of the
    # ENVIRONMENT above (it reads the current value and re-sets it, so order matters: this
    # must come after set_tests_properties).
    setup_qa_env( ${test_name} )
endmacro()

# 467 checks, no build required: this is the one that can run in a no-C++ CI job.
phicad_add_mcp_test( qa_mcp_registry  test_registry.py  600 )

# --quick is the ~30 s subset (no exports, no IPC, no jobset run).  The full 206-check run
# needs minutes and a warm kicad-cli; keep it a manual `phicad mcp selftest --full`.
phicad_add_mcp_test( qa_mcp_tools     test_tools.py     900 --quick )

# Spawns server.py over stdio through fastmcp's client and walks all 16 tool sections.
phicad_add_mcp_test( qa_mcp_handshake test_handshake.py 900 )

message( STATUS "phicad MCP suites registered (interpreter: ${MCP_VENV_PYTHON})" )
