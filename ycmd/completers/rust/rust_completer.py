# Copyright (C) 2015 ycmd contributors
#
# This file is part of ycmd.
#
# ycmd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ycmd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ycmd.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import *  # noqa
from future.utils import native, iteritems
from future import standard_library
standard_library.install_aliases()

from ycmd.utils import ToBytes, SetEnviron, ProcessIsRunning
from ycmd.completers.completer import Completer
from ycmd import responses, utils, hmac_utils

import logging
import urllib.parse
import requests
import json
import tempfile
import base64
import binascii
import threading
import os

from os import path as p

_logger = logging.getLogger( __name__ )

DIR_OF_THIS_SCRIPT = p.dirname( p.abspath( __file__ ) )
DIR_OF_THIRD_PARTY = p.join( DIR_OF_THIS_SCRIPT, '..', '..', '..',
                             'third_party' )

RACERD_BINARY_NAME = 'racerd' + ( '.exe' if utils.OnWindows() else '' )
RACERD_BINARY_RELEASE = p.join( DIR_OF_THIRD_PARTY, 'racerd', 'target',
                        'release', RACERD_BINARY_NAME )
RACERD_BINARY_DEBUG = p.join( DIR_OF_THIRD_PARTY, 'racerd', 'target',
                        'debug', RACERD_BINARY_NAME )

RACERD_HMAC_HEADER = 'x-racerd-hmac'
HMAC_SECRET_LENGTH = 16

BINARY_NOT_FOUND_MESSAGE = ( 'racerd binary not found. Did you build it? ' +
                             'You can do so by running ' +
                             '"./build.py --racer-completer".' )
ERROR_FROM_RACERD_MESSAGE = (
  'Received error from racerd while retrieving completions. You did not '
  'set the rust_src_path option, which is probably causing this issue. '
  'See YCM docs for details.'
)


def FindRacerdBinary( user_options ):
  """
  Find path to racerd binary

  This function prefers the 'racerd_binary_path' value as provided in
  user_options if available. It then falls back to ycmd's racerd build. If
  that's not found, attempts to use racerd from current path.
  """
  racerd_user_binary = user_options.get( 'racerd_binary_path' )
  if racerd_user_binary:
    # The user has explicitly specified a path.
    if os.path.isfile( racerd_user_binary ):
      return racerd_user_binary
    _logger.warning( 'User-provided racerd_binary_path does not exist.' )

  if os.path.isfile( RACERD_BINARY_RELEASE ):
    return RACERD_BINARY_RELEASE

  # We want to support using the debug binary for the sake of debugging; also,
  # building the release version on Travis takes too long.
  if os.path.isfile( RACERD_BINARY_DEBUG ):
    _logger.warning( 'Using racerd DEBUG binary; performance will suffer!' )
    return RACERD_BINARY_DEBUG

  return utils.PathToFirstExistingExecutable( [ 'racerd' ] )


class RustCompleter( Completer ):
  """
  A completer for the rust programming language backed by racerd.
  https://github.com/jwilm/racerd
  """

  def __init__( self, user_options ):
    super( RustCompleter, self ).__init__( user_options )
    self._racerd = FindRacerdBinary( user_options )
    self._racerd_host = None
    self._server_state_lock = threading.RLock()
    self._keep_logfiles = user_options[ 'server_keep_logfiles' ]
    self._hmac_secret = ''
    self._rust_source_path = self._GetRustSrcPath()

    if not self._rust_source_path:
      _logger.warning( 'No path provided for the rustc source. Please set the '
                       'rust_src_path option' )

    if not self._racerd:
      _logger.error( BINARY_NOT_FOUND_MESSAGE )
      raise RuntimeError( BINARY_NOT_FOUND_MESSAGE )

    self._StartServer()


  def _GetRustSrcPath( self ):
    """
    Attempt to read user option for rust_src_path. Fallback to environment
    variable if it's not provided.
    """
    rust_src_path = self.user_options[ 'rust_src_path' ]

    # Early return if user provided config
    if rust_src_path:
      return rust_src_path

    # Fall back to environment variable
    env_key = 'RUST_SRC_PATH'
    if env_key in os.environ:
      return os.environ[ env_key ]

    return None


  def SupportedFiletypes( self ):
    return [ 'rust' ]


  def _GetResponse( self, handler, request_data = None,
                    method = 'POST'):
    """
    Query racerd via HTTP

    racerd returns JSON with 200 OK responses. 204 No Content responses occur
    when no errors were encountered but no completions, definitions, or errors
    were found.
    """
    _logger.info( 'RustCompleter._GetResponse' )
    handler = ToBytes( handler )
    method = ToBytes( method )
    url = urllib.parse.urljoin( ToBytes( self._racerd_host ), handler )
    parameters = self._ConvertToRacerdRequest( request_data )
    body = ToBytes( json.dumps( parameters ) ) if parameters else bytes()
    extra_headers = self._ExtraHeaders( method, handler, body )

    _logger.debug( 'Making racerd request: %s %s %s %s', method, url,
                   extra_headers, body )

    # Failing to wrap the method & url bytes objects in `native()` causes HMAC
    # failures (403 Forbidden from racerd) for unknown reasons. Similar for
    # request_hmac above.
    response = requests.request( native( method ),
                                 native( url ),
                                 data = body,
                                 headers = extra_headers )

    response.raise_for_status()

    if response.status_code == requests.codes.no_content:
      return None

    return response.json()


  def _ExtraHeaders( self, method, handler, body ):
    if not body:
      body = bytes()

    hmac = hmac_utils.CreateRequestHmac( method,
                                         handler,
                                         body,
                                         self._hmac_secret )
    final_hmac_value = native( ToBytes( binascii.hexlify( hmac ) ) )

    extra_headers = { 'content-type': 'application/json' }
    extra_headers[ RACERD_HMAC_HEADER ] = final_hmac_value
    return extra_headers


  def _ConvertToRacerdRequest( self, request_data ):
    """
    Transform ycm request into racerd request
    """
    if not request_data:
      return None

    file_path = request_data[ 'filepath' ]
    buffers = []
    for path, obj in iteritems( request_data[ 'file_data' ] ):
      buffers.append( {
        'contents': obj[ 'contents' ],
        'file_path': path
      } )

    line = request_data[ 'line_num' ]
    col = request_data[ 'column_num' ] - 1

    return {
      'buffers': buffers,
      'line': line,
      'column': col,
      'file_path': file_path
    }


  def _GetExtraData( self, completion ):
    location = {}
    if completion[ 'file_path' ]:
      location[ 'filepath' ] = completion[ 'file_path' ]
    if completion[ 'line' ]:
      location[ 'line_num' ] = completion[ 'line' ]
    if completion[ 'column' ]:
      location[ 'column_num' ] = completion[ 'column' ] + 1

    if location:
      return { 'location': location }

    return None


  def ComputeCandidatesInner( self, request_data ):
    try:
      completions = self._FetchCompletions( request_data )
    except requests.HTTPError:
      if not self._rust_source_path:
        raise RuntimeError( ERROR_FROM_RACERD_MESSAGE )
      raise

    if not completions:
      return []

    return [ responses.BuildCompletionData(
                insertion_text = completion[ 'text' ],
                kind = completion[ 'kind' ],
                extra_menu_info = completion[ 'context' ],
                extra_data = self._GetExtraData( completion ) )
             for completion in completions ]


  def _FetchCompletions( self, request_data ):
    return self._GetResponse( '/list_completions', request_data )


  def _StartServer( self ):
    with self._server_state_lock:
      port = utils.GetUnusedLocalhostPort()
      self._hmac_secret = self._CreateHmacSecret()

      # racerd will delete the secret_file after it's done reading it
      with tempfile.NamedTemporaryFile( delete = False ) as secret_file:
        secret_file.write( self._hmac_secret )
        args = [ self._racerd, 'serve',
                '--port', str( port ),
                '-l',
                '--secret-file', secret_file.name ]

      # Enable logging of crashes
      env = os.environ.copy()
      SetEnviron( env, 'RUST_BACKTRACE', '1' )

      if self._rust_source_path:
        args.extend( [ '--rust-src-path', self._rust_source_path ] )

      filename_format = p.join( utils.PathToCreatedTempDir(),
                                'racerd_{port}_{std}.log' )

      self._server_stdout = filename_format.format( port = port,
                                                    std = 'stdout' )
      self._server_stderr = filename_format.format( port = port,
                                                    std = 'stderr' )

      with utils.OpenForStdHandle( self._server_stderr ) as fstderr:
        with utils.OpenForStdHandle( self._server_stdout ) as fstdout:
          self._racerd_phandle = utils.SafePopen( args,
                                                  stdout = fstdout,
                                                  stderr = fstderr,
                                                  env = env )

      self._racerd_host = 'http://127.0.0.1:{0}'.format( port )
      if not self.ServerIsRunning():
        raise RuntimeError( 'Failed to start racerd!' )
      _logger.info( 'Racerd started on: ' + self._racerd_host )


  def ServerIsRunning( self ):
    """
    Check if racerd is alive. That doesn't necessarily mean it's ready to serve
    requests; that's checked by ServerIsReady.
    """
    with self._server_state_lock:
      return ( bool( self._racerd_host ) and
               ProcessIsRunning( self._racerd_phandle ) )


  def ServerIsReady( self ):
    """
    Check if racerd is alive AND ready to serve requests.
    """
    if not self.ServerIsRunning():
      _logger.debug( 'Racerd not running.' )
      return False
    try:
      self._GetResponse( '/ping', method = 'GET' )
      return True
    # Do NOT make this except clause more generic! If you need to catch more
    # exception types, list them all out. Having `Exception` here caused FORTY
    # HOURS OF DEBUGGING.
    except requests.exceptions.ConnectionError as e:
      _logger.exception( e )
      return False


  def _StopServer( self ):
    with self._server_state_lock:
      if self._racerd_phandle:
        self._racerd_phandle.terminate()
        self._racerd_phandle.wait()
        self._racerd_phandle = None
        self._racerd_host = None

      if not self._keep_logfiles:
        # Remove stdout log
        if self._server_stdout and p.exists( self._server_stdout ):
          os.unlink( self._server_stdout )
          self._server_stdout = None

        # Remove stderr log
        if self._server_stderr and p.exists( self._server_stderr ):
          os.unlink( self._server_stderr )
          self._server_stderr = None


  def _RestartServer( self ):
    _logger.debug( 'RustCompleter restarting racerd' )

    with self._server_state_lock:
      if self.ServerIsRunning():
        self._StopServer()
      self._StartServer()

    _logger.debug( 'RustCompleter has restarted racerd' )


  def GetSubcommandsMap( self ):
    return {
      'GoTo' : ( lambda self, request_data, args:
                 self._GoToDefinition( request_data ) ),
      'GoToDefinition' : ( lambda self, request_data, args:
                           self._GoToDefinition( request_data ) ),
      'GoToDeclaration' : ( lambda self, request_data, args:
                           self._GoToDefinition( request_data ) ),
      'StopServer' : ( lambda self, request_data, args:
                           self._StopServer() ),
      'RestartServer' : ( lambda self, request_data, args:
                           self._RestartServer() ),
    }


  def _GoToDefinition( self, request_data ):
    try:
      definition = self._GetResponse( '/find_definition',
                                      request_data )
      return responses.BuildGoToResponse( definition[ 'file_path' ],
                                          definition[ 'line' ],
                                          definition[ 'column' ] + 1 )
    except Exception as e:
      _logger.exception( e )
      raise RuntimeError( 'Can\'t jump to definition.' )


  def Shutdown( self ):
    self._StopServer()


  def _CreateHmacSecret( self ):
    return base64.b64encode( os.urandom( HMAC_SECRET_LENGTH ) )


  def DebugInfo( self, request_data ):
    with self._server_state_lock:
      if self.ServerIsRunning():
        return ( 'racerd\n'
                 '  listening at: {0}\n'
                 '  racerd path: {1}\n'
                 '  stdout log: {2}\n'
                 '  stderr log: {3}').format( self._racerd_host,
                                              self._racerd,
                                              self._server_stdout,
                                              self._server_stderr )

      if self._server_stdout and self._server_stderr:
        return ( 'racerd is no longer running\n',
                 '  racerd path: {0}\n'
                 '  stdout log: {1}\n'
                 '  stderr log: {2}').format( self._racerd,
                                              self._server_stdout,
                                              self._server_stderr )

      return 'racerd is not running'
