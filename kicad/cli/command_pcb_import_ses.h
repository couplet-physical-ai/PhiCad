/*
 * This program source code file is part of KiCad, a free EDA CAD application.
 *
 * Copyright The KiCad Developers, see AUTHORS.txt for contributors.
 *
 * This program is free software: you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the
 * Free Software Foundation, either version 3 of the License, or (at your
 * option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#ifndef COMMAND_PCB_IMPORT_SES_H
#define COMMAND_PCB_IMPORT_SES_H

#include "command.h"

namespace CLI
{

/**
 * Merge a Specctra session file into a board.
 *
 * Named "import-ses" rather than a subcommand of "import" because `kicad-cli pcb import` is
 * already the leaf command that converts a non-KiCad board file.
 */
class PCB_IMPORT_SES_COMMAND : public COMMAND
{
public:
    PCB_IMPORT_SES_COMMAND();

protected:
    int doPerform( KIWAY& aKiway ) override;
};

}

#endif
