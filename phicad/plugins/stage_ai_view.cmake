# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.

# Build-time helper for phicad/plugins/CMakeLists.txt: stage the AI View
# plugin (runtime files only) and write the venv-path/repo-path markers that
# ai_view.sh and pylib_path.py resolve.  Runs as `cmake -P`, so it re-executes
# on every staging, keeping deleted source files from lingering in the stage.
#
# Expects: AI_VIEW_SRC, AI_VIEW_DEST, REPO_ROOT, VENV_PYTHON.

foreach( var AI_VIEW_SRC AI_VIEW_DEST REPO_ROOT VENV_PYTHON )
    if( NOT DEFINED ${var} )
        message( FATAL_ERROR "stage_ai_view.cmake: ${var} not set" )
    endif()
endforeach()

file( REMOVE_RECURSE ${AI_VIEW_DEST} )

file( COPY ${AI_VIEW_SRC}/
      DESTINATION ${AI_VIEW_DEST}
      PATTERN "__pycache__" EXCLUDE
      PATTERN "*.pyc" EXCLUDE
      PATTERN "test_*.py" EXCLUDE
      PATTERN "make_icons.py" EXCLUDE
      PATTERN ".DS_Store" EXCLUDE )

# Same marker contract as `phicad ai-view-install` (see ai-view/ai_view.sh):
# absolute pointers back to this checkout.  ai_view.sh vets the interpreter on
# every launch, so stale markers degrade to a clear error, never a hang.
file( WRITE ${AI_VIEW_DEST}/venv-path "${VENV_PYTHON}\n" )
file( WRITE ${AI_VIEW_DEST}/repo-path "${REPO_ROOT}\n" )
