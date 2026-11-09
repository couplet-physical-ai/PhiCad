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

#ifndef JOB_PCB_IMPORT_SES_H
#define JOB_PCB_IMPORT_SES_H

#include <kicommon.h>
#include "job.h"

/**
 * Merge a Specctra session (.ses) — an autorouter's answer to an exported .dsn — into a board.
 *
 * This job mutates the board it is given rather than producing a new artifact, so it is not in
 * the JOB_REGISTRY (jobsets model jobs as file producers); it exists for the CLI only, like
 * JOB_PCB_UPGRADE.
 */
class KICOMMON_API JOB_PCB_IMPORT_SES : public JOB
{
public:
    JOB_PCB_IMPORT_SES();

    wxString m_filename;

    /// Session file produced by the router from the board's exported .dsn.
    wxString m_sesFile;

    /// Board to write the merged result to.  Empty means overwrite m_filename in place.
    wxString m_outputFile;
};

#endif
