import http.client
import gzip
import json
import re
import socket
import threading
from urllib.parse import urlparse
import zlib

import sublime
import sublime_plugin


RE_METHOD = """(?P<method>[A-Z]+)"""
RE_URI = """(?P<uri>[a-zA-Z0-9\-\/\.\_\:\?\#\[\]\@\!\$\&\=]+)"""
RE_PROTOCOL = """(?P<protocol>.*)"""
RE_ENCODING = """(?:encoding|charset)=['"]*([a-zA-Z0-9\-]+)['"]*"""
SETTINGS_FILE = "RESTer.sublime-settings"


def scan_string_for_encoding(string):
    """Read a string and return the encoding identified within."""
    m = re.search(RE_ENCODING, string)
    if m:
        return m.groups()[0]
    return None


def scan_bytes_for_encoding(bytes):
    """Read a byte sequence and return the encoding identified within."""
    m = re.search(RE_ENCODING.encode('ascii'), bytes)
    if m:
        encoding = m.groups()[0]
        return encoding.decode('ascii')
    return None


def decode(bytes, encodings):
    """Return the first successfully decoded string or None"""
    for encoding in encodings:
        try:
            body = bytes.decode(encoding)
            return body
        except UnicodeDecodeError:
            # Try the next in the list.
            pass
    raise DecodeError


class InsertResponseCommand(sublime_plugin.TextCommand):
    """Output a response to a new file.

    This TextCommand is for internal use and not intended for end users.

    """

    def run(self, edit, status_line="", headers="", body="", eol="\n"):

        pos = 0
        start = 0
        end = 0

        if status_line:
            pos += self.view.insert(edit, pos, status_line + eol)
        if headers:
            pos += self.view.insert(edit, pos, headers + eol + eol)
        if body:
            start = pos
            pos += self.view.insert(edit, pos, body)
            end = pos

        # Select the inserted response body.
        if start != 0 and end != 0:
            selection = sublime.Region(start, end)
            self.view.sel().clear()
            self.view.sel().add(selection)


class ResterHttpRequestCommand(sublime_plugin.TextCommand):
    """Make an HTTP request. Display the response in a new file."""

    def run(self, edit):
        text = self._get_selection()
        eol = self._get_end_of_line_character()
        thread = HttpRequestThread(text, eol)
        thread.start()
        self._handle_thread(thread)

    def _handle_thread(self, thread, i=0, dir=1):

        if thread.is_alive():
            # This animates a little activity indicator in the status area.
            before = i % 8
            after = 7 - before
            if not after:
                dir = -1
            if not before:
                dir = 1
            i += dir
            message = "RESTer [%s=%s]" % (" " * before, " " * after)
            self.view.set_status("rester", message)
            sublime.set_timeout(lambda:
                                self._handle_thread(thread, i, dir), 100)

        elif isinstance(thread.result, http.client.HTTPResponse):
            # Success.
            self._complete_thread(thread.result, thread.settings)
        elif isinstance(thread.result, str):
            # Failed.
            self.view.erase_status("rester")
            sublime.status_message(thread.result)
        else:
            # Failed.
            self.view.erase_status("rester")
            sublime.status_message("Unable to make request.")

    def _complete_thread(self, response, settings):

        # Create a new file.
        view = self.view.window().new_file()

        eol = self._get_end_of_line_character()

        # Build the status line (e.g., HTTP/1.1 200 OK)
        protocol = "HTTP"
        if response.version == 11:
            version = "1.1"
        else:
            version = "1.0"
        status_line = "%s/%s %d %s" % (protocol, version, response.status,
                                       response.reason)

        # Build the headers
        headers = []
        for (key, value) in response.getheaders():
            headers.append("%s: %s" % (key, value))
        headers = eol.join(headers)

        # Decode the body from a list of bytes
        body_bytes = response.read()

        # Unzip if needed.
        content_encoding = response.getheader("content-encoding")
        if content_encoding:
            content_encoding = content_encoding.lower()
            if "gzip" in content_encoding:
                body_bytes = gzip.decompress(body_bytes)
            elif "deflate" in content_encoding:
                # Negatie wbits to supress the standard gzip header.
                body_bytes = zlib.decompress(body_bytes, -15)

        # Decode the body. The hard part here is finding the right encoding.
        # To do this, create a list of possible matches.
        encodings = []

        # Check the content-type header, if present.
        content_type = response.getheader("content-type")
        if content_type:
            encoding = scan_string_for_encoding(content_type)
            if encoding:
                encodings.append(encoding)

        # Scan the body
        encoding = scan_bytes_for_encoding(body_bytes)
        if encoding:
            encodings.append(encoding)

        # Add any default encodings not already discovered.
        default_encodings = settings.get("default_response_encodings", [])
        for encoding in default_encodings:
            if encoding not in encodings:
                encodings.append(encoding)

        # Decoding using the encodings discovered.
        try:
            body = decode(body_bytes, encodings)
        except DecodeError:
            body = "{Unable to decode body}"

        # Normalize the line endings
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        eol = self._get_end_of_line_character()
        if eol != "\n":
            body = body.replace("\n", eol)

        # Insert the response and select the body.
        if settings.get("body_only") and 200 <= response.status <= 299:
            # Output the body only, but only on success.
            view.run_command("insert_response", {
                "body": body,
                "eol": eol
            })
        else:
            # Output status, headers, and body.
            view.run_command("insert_response", {
                "status_line": status_line,
                "headers": headers,
                "body": body,
                "eol": eol
            })

        # Read the content-type header, if present.
        actual_content_type = response.getheader("content-type")
        if actual_content_type:
            actual_content_type = actual_content_type.lower()

        # Run commands on the inserted body
        command_list = settings.get("response_body_commands", [])
        for command in command_list:

            # Run by default, unless there is a content-type member to filter.
            run = True

            # If this command has a content-type list, only run if the
            # actual content type matches an item in the list.
            if "content-type" in command:
                run = False
                if actual_content_type:
                    test_types = command["content-type"]

                    # String: Test if match.
                    if isinstance(test_types, str):
                        run = actual_content_type == test_types.lower()

                    # Iterable: Test if contained.
                    # Check iterable for stringness of all items.
                    # Will raise TypeError if some_object is not iterable
                    elif all(isinstance(item, str) for item in test_types):
                        test_types = [test.lower() for test in test_types]
                        run = actual_content_type in test_types

                    else:
                        raise TypeError

            if run:
                for commandName in command["commands"]:
                    view.run_command(commandName)

        # Write the status message.
        self.view.erase_status("rester")
        sublime.status_message("RESTer Request Complete")

    def _get_selection(self):
        """Return the selected text or the entire buffer."""
        sels = self.view.sel()
        if len(sels) == 1 and sels[0].empty():
            # No selection. Use the entire buffer.
            selection = self.view.substr(sublime.Region(0, self.view.size()))
        else:
            # Concatenate the selections into one large string.
            selection = ""
            for sel in sels:
                selection += self.view.substr(sel)
        return selection

    def _get_end_of_line_character(self):
        """Return the EOL character from the view's settings."""
        line_endings = self.view.settings().get("default_line_ending")
        if line_endings == "windows":
            return "\r\n"
        elif line_endings == "mac":
            return "\r"
        else:
            return "\n"


class HttpRequestThread(threading.Thread):
    """Thread sublcass for making an HTTP request given a string."""

    def __init__(self, string, eol="\n"):
        """Create a new request object"""

        threading.Thread.__init__(self)

        # Load the settings
        settings = sublime.load_settings(SETTINGS_FILE)

        # Store members and set defaults.
        self.settings = OverrideableSettings(settings)
        self._eol = eol
        self._scheme = "http"
        self._hostname = None
        self._path = None
        self._query = None
        self._method = "GET"
        self._header_lines = []
        self._headers = settings.get("default_headers", {})
        if not isinstance(self._headers, dict):
            self._headers = {}
        self._body = None

        # Parse the string to fill in the members with actual values.
        self._parse_string(string)

    def run(self):
        """Method to run when the thread is started."""

        # Fail if the hostname is not set.
        if not self._hostname:
            self.result = "Unable to make request: Please provide a hostname."
            return

        # Create the connection.
        timeout = self.settings.get("timeout")
        conn = http.client.HTTPConnection(self._hostname, timeout=timeout)

        try:
            conn.request(self._method,
                         self._get_requet_uri(),
                         headers=self._headers,
                         body=self._body)
        except socket.gaierror:
            self.result = "Unable to make request. "
            self.result += "Make sure the hostname is valid."
            conn.close()
            return

        # Output the request to the console.
        if self.settings.get("output_request", True):
            print(self._get_request_as_string())

        # Read the response.
        try:
            resp = conn.getresponse()
        except socket.timeout:
            self.result = "Request timed out."
            conn.close()
            return

        conn.close()
        self.result = resp

    def _get_requet_uri(self):
        """Return the path + query string for the request."""
        uri = self._path
        if self._query:
            uri += "?" + self._query
        return uri

    def _get_request_as_string(self):
        """Return a string representation of the request."""

        lines = []

        # TODO Allow for HTTPS
        protocol = "HTTP/1.1"

        lines.append("%s %s %s" % (self._method,
                                   self._get_requet_uri(),
                                   protocol))

        for key in self._headers:
            lines.append("%s: %s" % (key, self._headers[key]))

        string = self._eol.join(lines)

        if self._body:
            string += self._eol + self._body

        return string

    def _normalize_line_endings(self, string):
        """Return a string with consistent line endings."""
        string = string.replace("\r\n", "\n").replace("\r", "\n")
        if self._eol != "\n":
            string = string.replace("\n", self._eol)
        return string

    def _parse_string(self, string):
        """Determine instance members from the contents of the string."""

        # Pre-parse clean-up.
        string = string.lstrip()
        string = self._normalize_line_endings(string)

        # Split the string into lines.
        lines = string.split(self._eol)

        # The first line is the request line.
        request_line = lines[0]

        # Parse the first line as the request line.
        self._parse_request_line(request_line)

        # All lines following the request line are headers until an empty line.
        # All content after the empty line is the request body.
        has_body = False
        for i in range(1, len(lines)):
            if lines[i] == "":
                has_body = True
                break

        if has_body:
            self._header_lines = lines[1:i]
            self._body = self._eol.join(lines[i+1:])
        else:
            self._header_lines = lines[1:]

        # Make a dictionary of headers.
        self._parse_header_lines()

        # Check if a Host header was supplied.
        has_host_header = False
        for key in self._headers:
            if key.lower() == "host":
                has_host_header = True
                # If self._hostname is not yet set, read it from the header.
                if not self._hostname:
                    self._hostname = self._headers[key]
                break

        # Add a host header, if not explicitly set.
        if not has_host_header and self._hostname:
            self._headers["Host"] = self._hostname

        # If there is still no hostname, but there is a path, try re-parsing
        # the path with // prepended.
        #
        # From the Python documentation:
        # Following the syntax specifications in RFC 1808, urlparse recognizes
        # a netloc only if it is properly introduced by ‘//’. Otherwise the
        # input is presumed to be a relative URL and thus to start with a path
        # component.
        #
        if not self._hostname and self._path:
            uri = urlparse("//" + self._path)
            self._hostname = uri.hostname
            self._path = uri.path

    def _parse_request_line(self, line):
        """Parse the first line of the request"""

        # Parse the first line as the request line.
        # Fail, if unable to parse.
        request_line = self._read_request_line_dict(line)
        if not request_line:
            return

        # Parse the URI.
        uri = urlparse(request_line["uri"])

        # Copy from the parsed URI.
        self._scheme = uri.scheme
        self._hostname = uri.hostname
        self._path = uri.path
        self._query = uri.query

        # Read the method from the request line. Default is GET.
        if "method" in request_line:
            self._method = request_line["method"]

        # Read the scheme from the URI or request line. Default is http.
        if not self._scheme:
            if "protocol" in request_line:
                protocol = request_line["protocol"].upper()
                if "HTTPS" in protocol:
                    self._scheme = "https"

    def _parse_header_lines(self):
        """Parse the lines before the body.

        Build self._headers dictionary
        Read the overrides for the settings

        """

        headers = {}
        overrides = {}

        for header in self._header_lines:
            header = header.lstrip()

            # Comments begin with #
            if header[0] == "#":
                pass

            # Overrides begin with @
            elif header[0] == "@" and ":" in header:
                (key, value) = header[1:].split(":", 1)
                overrides[key.strip()] = json.loads(value.strip())

            # All else are headers
            elif ":" in header:
                (key, value) = header.split(":", 1)
                headers[key] = value.strip()

        if headers and self._headers:
            self._headers = dict(list(self._headers.items()) +
                                 list(headers.items()))

        self.settings.set_overrides(overrides)

    def _read_request_line_dict(self, line):
        """Return a dicionary containing information about the request."""

        # method-uri-protocol
        # Ex: GET /path HTTP/1.1
        m = re.search(RE_METHOD + " " + RE_URI + " " + RE_PROTOCOL, line)
        if m:
            return m.groupdict()

        # method-uri
        # Ex: GET /path HTTP/1.1
        m = re.search(RE_METHOD + " " + RE_URI, line)
        if m:
            return m.groupdict()

        # uri
        # Ex: /path or http://hostname/path
        m = re.search(RE_URI, line)
        if m:
            return m.groupdict()

        return None


class OverrideableSettings():
    """
    Class for adding a layer of overrides on top of a Settings object

    The class is read-only. If a dictionary-like _overrides member is present,
    the get() method will look there first for a setting before reading from
    the _settings member.
    """

    def __init__(self, settings=None, overrides=None):
        self._settings = settings
        self._overrides = overrides

    def set_settings(self, settings):
        self._settings = settings

    def set_overrides(self, overrides):
        self._overrides = overrides

    def get(self, setting, default=None):
        if self._overrides and setting in self._overrides:
            return self._overrides[setting]
        elif self._settings:
            return self._settings.get(setting, default)
        else:
            return default


class DecodeError(Exception):
    pass