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

#ifndef JOB_EXPORT_PCB_DSN_H
#define JOB_EXPORT_PCB_DSN_H

#include <kicommon.h>
#include "job.h"

/**
 * Export a board as a Specctra DSN design, the input side of an external autorouter.
 *
 * The exporter itself (DSN::ExportBoardToSpecctraFile) takes no options, so this job carries
 * only the board path; the output path comes from JOB's configured/working output path.
 */
class KICOMMON_API JOB_EXPORT_PCB_DSN : public JOB
{
public:
    JOB_EXPORT_PCB_DSN();

    wxString GetDefaultDescription() const override;
    wxString GetSettingsDialogTitle() const override;

    wxString m_filename;
};

#endif
