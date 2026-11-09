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

#include "command_pcb_export_dsn.h"
#include <cli/exit_codes.h>
#include "jobs/job_export_pcb_dsn.h"
#include <kiface_base.h>
#include <string_utils.h>
#include <wx/crt.h>
#include <wx/file.h>


CLI::PCB_EXPORT_DSN_COMMAND::PCB_EXPORT_DSN_COMMAND() :
        PCB_EXPORT_BASE_COMMAND( "dsn", IO_TYPE::FILE, IO_TYPE::FILE )
{
    m_argParser.add_description(
            UTF8STDSTR( _( "Export the PCB as a Specctra DSN design for an external autorouter" ) ) );

    addDefineArg();
}


int CLI::PCB_EXPORT_DSN_COMMAND::doPerform( KIWAY& aKiway )
{
    std::unique_ptr<JOB_EXPORT_PCB_DSN> dsnJob( new JOB_EXPORT_PCB_DSN() );

    dsnJob->m_filename = m_argInput;
    dsnJob->SetConfiguredOutputPath( m_argOutput );
    dsnJob->SetVarOverrides( m_argDefineVars );

    if( !wxFile::Exists( dsnJob->m_filename ) )
    {
        wxFprintf( stderr, _( "Board file does not exist or is not accessible\n" ) );
        return EXIT_CODES::ERR_INVALID_INPUT_FILE;
    }

    return aKiway.ProcessJob( KIWAY::FACE_PCB, dsnJob.get() );
}
