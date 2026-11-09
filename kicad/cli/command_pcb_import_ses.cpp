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

#include "command_pcb_import_ses.h"
#include <cli/exit_codes.h>
#include <jobs/job_pcb_import_ses.h>
#include <string_utils.h>
#include <wx/crt.h>
#include <wx/file.h>

#define ARG_SESSION "--ses"


CLI::PCB_IMPORT_SES_COMMAND::PCB_IMPORT_SES_COMMAND() : COMMAND( "import-ses" )
{
    addCommonArgs( true, true, IO_TYPE::FILE, IO_TYPE::FILE );

    m_argParser.add_description(
            UTF8STDSTR( _( "Merge a Specctra session file (routed tracks and vias) into a board" ) ) );

    m_argParser.add_argument( ARG_SESSION )
            .default_value( std::string() )
            .help( UTF8STDSTR( _( "Specctra session file (.ses) to merge" ) ) )
            .metavar( "SES_FILE" );
}


int CLI::PCB_IMPORT_SES_COMMAND::doPerform( KIWAY& aKiway )
{
    std::unique_ptr<JOB_PCB_IMPORT_SES> sesJob = std::make_unique<JOB_PCB_IMPORT_SES>();

    sesJob->m_filename = m_argInput;
    sesJob->m_sesFile = From_UTF8( m_argParser.get<std::string>( ARG_SESSION ).c_str() );
    sesJob->m_outputFile = m_argOutput;

    if( sesJob->m_sesFile.IsEmpty() )
    {
        wxFprintf( stderr, _( "A session file is required (%s)\n" ), wxS( ARG_SESSION ) );
        return EXIT_CODES::ERR_ARGS;
    }

    if( !wxFile::Exists( sesJob->m_filename ) )
    {
        wxFprintf( stderr, _( "Board file does not exist or is not accessible\n" ) );
        return EXIT_CODES::ERR_INVALID_INPUT_FILE;
    }

    if( !wxFile::Exists( sesJob->m_sesFile ) )
    {
        wxFprintf( stderr, _( "Session file does not exist or is not accessible\n" ) );
        return EXIT_CODES::ERR_INVALID_INPUT_FILE;
    }

    return aKiway.ProcessJob( KIWAY::FACE_PCB, sesJob.get() );
}
