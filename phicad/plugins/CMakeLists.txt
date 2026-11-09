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

# Ship the AI View plugin INSIDE the application, so the AI surface exists in
# every build of PhiCad with zero install steps.
#
# Why this works with no C++ change: API_PLUGIN_MANAGER scans three places for
# `plugin.json` manifests (common/api/api_plugin_manager.cpp) and one of them
# is PATHS::GetStockPluginsPath() — on macOS `<bundle>/Contents/SharedSupport/
# plugins/`, resolved with aRespectRunFromBuildDir=false, so both a build-tree
# run (KICAD_RUN_FROM_BUILD_DIR=1) and an installed app read the same location
# inside the bundle.  Putting the plugin there at build time makes the AI
# toolbar button a stock part of the application, exactly like the 3D plugins
# this directory's parent builds.  `phicad ai-view-install` (a per-user copy
# under ~/Documents/KiCad/<ver>/plugins/) remains as an override for hacking
# on the plugin without rebuilding; the manager loads both, and identical
# identifiers mean the user copy simply shadows the stock one in the UI.
#
# Two marker files are generated next to the copy (same contract as the
# installer writes, documented in ai-view/ai_view.sh and pylib_path.py):
#
#   venv-path   absolute path of the repo venv's python (kipy + Tk >= 8.6);
#               ai_view.sh vets it and falls back loudly if it is stale
#   repo-path   this checkout, so the plugin finds phicad/pylib + phicad/synth
#
# They point at THIS checkout, which is correct for every audience this fork
# has today (developers running their own builds).  ai_view.sh re-vets the
# interpreter on every launch, so a moved/deleted checkout degrades to a clear
# error dialog, never a hang.

set( AI_VIEW_SRC ${CMAKE_CURRENT_SOURCE_DIR}/ai-view )
set( AI_VIEW_STAGE_DIR ${CMAKE_BINARY_DIR}/phicad-plugins/ai-view )

# Everything the running plugin needs; tests and asset generators stay out of
# the application bundle.  CONFIGURE_DEPENDS keeps the list honest when files
# are added — at worst a re-run of cmake, never a stale bundle.
file( GLOB AI_VIEW_SOURCES CONFIGURE_DEPENDS
      ${AI_VIEW_SRC}/*.py
      ${AI_VIEW_SRC}/*.mjs
      ${AI_VIEW_SRC}/*.sh
      ${AI_VIEW_SRC}/plugin.json
      ${AI_VIEW_SRC}/README.md
      ${AI_VIEW_SRC}/icons/*.png )
list( FILTER AI_VIEW_SOURCES EXCLUDE REGEX "/(test_[^/]*|make_icons)\\.py$" )

# Stage the filtered tree plus the two markers, then mirror the stage into the
# bundle.  The stamp makes this incremental: an untouched plugin costs nothing.
set( AI_VIEW_STAMP ${CMAKE_BINARY_DIR}/phicad-plugins/ai-view.stamp )

add_custom_command(
    OUTPUT ${AI_VIEW_STAMP}
    COMMAND ${CMAKE_COMMAND}
            -DAI_VIEW_SRC=${AI_VIEW_SRC}
            -DAI_VIEW_DEST=${AI_VIEW_STAGE_DIR}
            -DREPO_ROOT=${PROJECT_SOURCE_DIR}
            -DVENV_PYTHON=${PROJECT_SOURCE_DIR}/build/venv/bin/python
            -P ${CMAKE_CURRENT_SOURCE_DIR}/stage_ai_view.cmake
    COMMAND ${CMAKE_COMMAND} -E touch ${AI_VIEW_STAMP}
    DEPENDS ${AI_VIEW_SOURCES} ${CMAKE_CURRENT_SOURCE_DIR}/stage_ai_view.cmake
    COMMENT "Staging AI View plugin into the application"
    VERBATIM )

add_custom_target( phicad_ai_view_plugin ALL DEPENDS ${AI_VIEW_STAMP} )

if( APPLE )
    # The build-tree bundle's Contents/SharedSupport is a symlink to
    # CMAKE_BINARY_DIR.  Copy to that destination directly: writing through
    # the not-yet-created bundle path races kicad's POST_BUILD symlink and can
    # leave a real SharedSupport directory that makes the full build fail.
    # `make install` below covers the installed bundle.
    add_custom_command(
        TARGET phicad_ai_view_plugin POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_directory
                ${AI_VIEW_STAGE_DIR}
                ${CMAKE_BINARY_DIR}/plugins/ai-view
        COMMENT "Bundling AI View plugin into ${OSX_BUNDLE_MAIN}"
        VERBATIM )
endif()

install( DIRECTORY ${AI_VIEW_STAGE_DIR}
         DESTINATION ${KICAD_PLUGINS}
         COMPONENT binary
         USE_SOURCE_PERMISSIONS )
