import sys

from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.gateway import WrappedDefault, interp2app, unwrap_spec
from pypy.interpreter.typedef import (
    GetSetProperty, TypeDef, generic_new_descr, interp_attrproperty,
    interp_attrproperty_w)
from pypy.module._codecs import interp_codecs
from pypy.module._io.interp_iobase import W_IOBase, convert_size, trap_eintr
from rpython.rlib.rarithmetic import intmask, r_uint, r_ulonglong
from rpython.rlib.rbigint import rbigint
from rpython.rlib.rstring import StringBuilder
from rpython.rlib.rutf8 import (check_utf8, next_codepoint_pos,
                                codepoints_in_utf8, get_utf8_length,
                                Utf8StringBuilder)


STATE_ZERO, STATE_OK, STATE_DETACHED = range(3)

SEEN_CR   = 1
SEEN_LF   = 2
SEEN_CRLF = 4
SEEN_ALL  = SEEN_CR | SEEN_LF | SEEN_CRLF

_WINDOWS = sys.platform == 'win32'

class W_IncrementalNewlineDecoder(W_Root):
    seennl = 0
    pendingcr = False
    w_decoder = None

    def __init__(self, space):
        self.w_newlines_dict = {
            SEEN_CR: space.newutf8("\r", 1),
            SEEN_LF: space.newutf8("\n", 1),
            SEEN_CRLF: space.newutf8("\r\n", 2),
            SEEN_CR | SEEN_LF: space.newtuple(
                [space.newutf8("\r", 1),
                 space.newutf8("\n", 1)]),
            SEEN_CR | SEEN_CRLF: space.newtuple(
                [space.newutf8("\r", 1),
                 space.newutf8("\r\n", 2)]),
            SEEN_LF | SEEN_CRLF: space.newtuple(
                [space.newutf8("\n", 1),
                 space.newutf8("\r\n", 2)]),
            SEEN_CR | SEEN_LF | SEEN_CRLF: space.newtuple(
                [space.newutf8("\r", 1),
                 space.newutf8("\n", 1),
                 space.newutf8("\r\n", 2)]),
            }

    @unwrap_spec(translate=int)
    def descr_init(self, space, w_decoder, translate, w_errors=None):
        self.w_decoder = w_decoder
        self.translate = translate
        if space.is_none(w_errors):
            self.w_errors = space.newtext("strict")
        else:
            self.w_errors = w_errors

        self.seennl = 0

    def newlines_get_w(self, space):
        return self.w_newlines_dict.get(self.seennl, space.w_None)

    @unwrap_spec(final=int)
    def decode_w(self, space, w_input, final=False):
        if self.w_decoder is None:
            raise oefmt(space.w_ValueError,
                        "IncrementalNewlineDecoder.__init__ not called")

        # decode input (with the eventual \r from a previous pass)
        if not space.is_w(self.w_decoder, space.w_None):
            w_output = space.call_method(self.w_decoder, "decode",
                                         w_input, space.newbool(bool(final)))
        else:
            w_output = w_input

        if not space.isinstance_w(w_output, space.w_unicode):
            raise oefmt(space.w_TypeError,
                        "decoder should return a string result")

        output, output_len = space.utf8_len_w(w_output)
        output_len = len(output)
        if self.pendingcr and (final or output_len):
            output = '\r' + output
            self.pendingcr = False
            output_len += 1

        # retain last \r even when not translating data:
        # then readline() is sure to get \r\n in one pass
        if not final and output_len > 0:
            last = len(output) - 1
            assert last >= 0
            if output[last] == '\r':
                output = output[:last]
                self.pendingcr = True
                output_len -= 1

        if output_len == 0:
            return space.newutf8("", 0)

        # Record which newlines are read and do newline translation if
        # desired, all in one pass.
        seennl = self.seennl

        if output.find('\r') < 0:
            # If no \r, quick scan for a possible "\n" character.
            # (there's nothing else to be done, even when in translation mode)
            if output.find('\n') >= 0:
                seennl |= SEEN_LF
                # Finished: we have scanned for newlines, and none of them
                # need translating.
        elif not self.translate:
            i = 0
            while i < len(output):
                if seennl == SEEN_ALL:
                    break
                c = output[i]
                i += 1
                if c == '\n':
                    seennl |= SEEN_LF
                elif c == '\r':
                    if i < len(output) and output[i] == '\n':
                        seennl |= SEEN_CRLF
                        i += 1
                    else:
                        seennl |= SEEN_CR
        elif output.find('\r') >= 0:
            # Translate!
            builder = StringBuilder(len(output))
            i = 0
            while i < output_len:
                c = output[i]
                i += 1
                if c == '\n':
                    seennl |= SEEN_LF
                elif c == '\r':
                    if i < len(output) and output[i] == '\n':
                        seennl |= SEEN_CRLF
                        i += 1
                    else:
                        seennl |= SEEN_CR
                    builder.append('\n')
                    continue
                builder.append(c)
            output = builder.build()

        self.seennl |= seennl
        lgt = check_utf8(output, True)
        return space.newutf8(output, lgt)

    def reset_w(self, space):
        self.seennl = 0
        self.pendingcr = False
        if self.w_decoder and not space.is_w(self.w_decoder, space.w_None):
            space.call_method(self.w_decoder, "reset")

    def getstate_w(self, space):
        if self.w_decoder and not space.is_w(self.w_decoder, space.w_None):
            w_state = space.call_method(self.w_decoder, "getstate")
            w_buffer, w_flag = space.unpackiterable(w_state, 2)
            flag = space.r_longlong_w(w_flag)
        else:
            w_buffer = space.newbytes("")
            flag = 0
        flag <<= 1
        if self.pendingcr:
            flag |= 1
        return space.newtuple([w_buffer, space.newint(flag)])

    def setstate_w(self, space, w_state):
        w_buffer, w_flag = space.unpackiterable(w_state, 2)
        flag = space.r_longlong_w(w_flag)
        self.pendingcr = bool(flag & 1)
        flag >>= 1

        if self.w_decoder and not space.is_w(self.w_decoder, space.w_None):
            w_state = space.newtuple([w_buffer, space.newint(flag)])
            space.call_method(self.w_decoder, "setstate", w_state)

W_IncrementalNewlineDecoder.typedef = TypeDef(
    '_io.IncrementalNewlineDecoder',
    __new__ = generic_new_descr(W_IncrementalNewlineDecoder),
    __init__  = interp2app(W_IncrementalNewlineDecoder.descr_init),

    decode = interp2app(W_IncrementalNewlineDecoder.decode_w),
    reset = interp2app(W_IncrementalNewlineDecoder.reset_w),
    getstate = interp2app(W_IncrementalNewlineDecoder.getstate_w),
    setstate = interp2app(W_IncrementalNewlineDecoder.setstate_w),

    newlines = GetSetProperty(W_IncrementalNewlineDecoder.newlines_get_w),
)

class W_TextIOBase(W_IOBase):
    w_encoding = None

    def __init__(self, space):
        W_IOBase.__init__(self, space)

    def read_w(self, space, w_size=None):
        self._unsupportedoperation(space, "read")

    def readline_w(self, space, w_limit=None):
        self._unsupportedoperation(space, "readline")

    def write_w(self, space, w_data):
        self._unsupportedoperation(space, "write")

    def detach_w(self, space):
        self._unsupportedoperation(space, "detach")

    def errors_get_w(self, space):
        return space.w_None

    def newlines_get_w(self, space):
        return space.w_None

W_TextIOBase.typedef = TypeDef(
    '_io._TextIOBase', W_IOBase.typedef,
    __new__ = generic_new_descr(W_TextIOBase),

    read = interp2app(W_TextIOBase.read_w),
    readline = interp2app(W_TextIOBase.readline_w),
    write = interp2app(W_TextIOBase.write_w),
    detach = interp2app(W_TextIOBase.detach_w),
    encoding = interp_attrproperty_w("w_encoding", W_TextIOBase),
    newlines = GetSetProperty(W_TextIOBase.newlines_get_w),
    errors = GetSetProperty(W_TextIOBase.errors_get_w),
)


def _determine_encoding(space, encoding):
    if encoding is not None:
        return space.newtext(encoding)

    try:
        w_locale = space.call_method(space.builtin, '__import__',
                                     space.newtext('locale'))
        w_encoding = space.call_method(w_locale, 'getpreferredencoding')
    except OperationError as e:
        # getpreferredencoding() may also raise ImportError
        if not e.match(space, space.w_ImportError):
            raise
        return space.newtext('ascii')
    else:
        if space.isinstance_w(w_encoding, space.w_text):
            return w_encoding

    raise oefmt(space.w_IOError, "could not determine default encoding")

class PositionCookie(object):
    def __init__(self, bigint):
        self.start_pos = bigint.ulonglongmask()
        bigint = bigint.rshift(r_ulonglong.BITS)
        x = intmask(bigint.uintmask())
        assert x >= 0
        self.dec_flags = x
        bigint = bigint.rshift(r_uint.BITS)
        x = intmask(bigint.uintmask())
        assert x >= 0
        self.bytes_to_feed = x
        bigint = bigint.rshift(r_uint.BITS)
        x = intmask(bigint.uintmask())
        assert x >= 0
        self.chars_to_skip = x
        bigint = bigint.rshift(r_uint.BITS)
        self.need_eof = bigint.tobool()

    def pack(self):
        # The meaning of a tell() cookie is: seek to position, set the
        # decoder flags to dec_flags, read bytes_to_feed bytes, feed them
        # into the decoder with need_eof as the EOF flag, then skip
        # chars_to_skip characters of the decoded result.  For most simple
        # decoders, tell() will often just give a byte offset in the file.
        rb = rbigint.fromrarith_int

        res = rb(self.start_pos)
        bits = r_ulonglong.BITS
        res = res.or_(rb(r_uint(self.dec_flags)).lshift(bits))
        bits += r_uint.BITS
        res = res.or_(rb(r_uint(self.bytes_to_feed)).lshift(bits))
        bits += r_uint.BITS
        res = res.or_(rb(r_uint(self.chars_to_skip)).lshift(bits))
        bits += r_uint.BITS
        return res.or_(rb(r_uint(self.need_eof)).lshift(bits))

class PositionSnapshot:
    def __init__(self, flags, input):
        self.flags = flags
        self.input = input


class DecodeBuffer(object):
    def __init__(self, text=None):
        self.text = text
        self.pos = 0
        self.upos = 0

    def set(self, space, w_decoded):
        check_decoded(space, w_decoded)
        self.text = space.utf8_w(w_decoded)
        self.pos = 0
        self.upos = 0

    def reset(self):
        self.text = None
        self.pos = 0
        self.upos = 0

    def get_chars(self, size):
        if self.text is None or size == 0:
            return ""

        lgt = codepoints_in_utf8(self.text)
        available = lgt - self.upos
        if size < 0 or size > available:
            size = available
        assert size >= 0

        if self.pos > 0 or size < available:
            start = self.pos
            ret = []
            pos = start
            for  i in range(size):
                pos = next_codepoint_pos(self.text, pos)
                self.upos += 1
            assert start >= 0
            assert pos >= 0
            chars = self.text[start:pos]
            self.pos = pos
        else:
            chars = self.text
            self.pos = len(self.text)
            self.upos = lgt

        return chars

    def has_data(self):
        return (self.text is not None and not self.exhausted())

    def exhausted(self):
        return self.pos >= len(self.text)

    def next_char(self):
        if self.exhausted():
            raise StopIteration
        newpos = next_codepoint_pos(self.text, self.pos)
        pos = self.pos
        assert pos >= 0
        assert newpos >= 0
        ch = self.text[pos:newpos]
        self.pos = newpos
        self.upos += 1
        return ch

    def peek_char(self):
        # like next_char, but doesn't advance pos
        if self.exhausted():
            raise StopIteration
        newpos = next_codepoint_pos(self.text, self.pos)
        pos = self.pos
        assert pos >= 0
        assert newpos >= 0
        return self.text[pos:newpos]

    def find_newline_universal(self, limit):
        # Universal newline search. Find any of \r, \r\n, \n
        # The decoder ensures that \r\n are not split in two pieces
        if limit < 0:
            limit = sys.maxint
        scanned = 0
        while scanned < limit:
            try:
                ch = self.next_char()
                scanned += 1
            except StopIteration:
                return False
            if ch == '\n':
                return True
            if ch == '\r':
                if scanned >= limit:
                    return False
                try:
                    ch = self.peek_char()
                except StopIteration:
                    return False
                if ch == '\n':
                    self.next_char()
                    return True
                else:
                    return True
        return False

    def find_crlf(self, limit):
        if limit < 0:
            limit = sys.maxint
        scanned = 0
        while scanned < limit:
            try:
                ch = self.next_char()
            except StopIteration:
                return False
            scanned += 1
            if ch == '\r':
                if scanned >= limit:
                    return False
                try:
                    if self.peek_char() == '\n':
                        self.next_char()
                        return True
                except StopIteration:
                    # This is the tricky case: we found a \r right at the end
                    self.pos -= 1
                    self.upos -= 1
                    return False
        return False

    def find_char(self, marker, limit):
        if limit < 0:
            limit = sys.maxint
        scanned = 0
        while scanned < limit:
            try:
                ch = self.next_char()
            except StopIteration:
                return False
            if ch == marker:
                return True
            scanned += 1
        return False


def check_decoded(space, w_decoded):
    if not space.isinstance_w(w_decoded, space.w_unicode):
        msg = "decoder should return a string result, not '%T'"
        raise oefmt(space.w_TypeError, msg, w_decoded)
    return w_decoded


class W_TextIOWrapper(W_TextIOBase):
    def __init__(self, space):
        W_TextIOBase.__init__(self, space)
        self.state = STATE_ZERO
        self.w_encoder = None
        self.w_decoder = None

        self.decoded = DecodeBuffer()
        self.pending_bytes = None   # list of bytes objects waiting to be
                                    # written, or NULL
        self.chunk_size = 8192

        self.readuniversal = False
        self.readtranslate = False
        self.readnl = None

        self.encodefunc = None # Specialized encoding func (see below)
        self.encoding_start_of_stream = False # Whether or not it's the start
                                              # of the stream
        self.snapshot = None

    @unwrap_spec(encoding="text_or_none", line_buffering=int)
    def descr_init(self, space, w_buffer, encoding=None,
                   w_errors=None, w_newline=None, line_buffering=0):
        self.state = STATE_ZERO
        self.w_buffer = w_buffer
        self.w_encoding = _determine_encoding(space, encoding)

        if space.is_none(w_errors):
            w_errors = space.newtext("strict")
        self.w_errors = w_errors

        if space.is_none(w_newline):
            newline = None
        else:
            newline = space.utf8_w(w_newline)
        if newline and newline not in ('\n', '\r\n', '\r'):
            raise oefmt(space.w_ValueError,
                        "illegal newline value: %R", w_newline)

        self.line_buffering = line_buffering

        self.readuniversal = not newline # null or empty
        self.readtranslate = newline is None
        self.readnl = newline

        self.writetranslate = (newline != '')
        if not self.readuniversal:
            self.writenl = self.readnl
            if self.writenl == '\n':
                self.writenl = None
        elif _WINDOWS:
            self.writenl = "\r\n"
        else:
            self.writenl = None

        # build the decoder object
        if space.is_true(space.call_method(w_buffer, "readable")):
            w_codec = interp_codecs.lookup_codec(space,
                                                 space.text_w(self.w_encoding))
            self.w_decoder = space.call_method(w_codec,
                                               "incrementaldecoder", w_errors)
            if self.readuniversal:
                self.w_decoder = space.call_function(
                    space.gettypeobject(W_IncrementalNewlineDecoder.typedef),
                    self.w_decoder, space.newbool(self.readtranslate))

        # build the encoder object
        if space.is_true(space.call_method(w_buffer, "writable")):
            w_codec = interp_codecs.lookup_codec(space,
                                                 space.text_w(self.w_encoding))
            self.w_encoder = space.call_method(w_codec,
                                               "incrementalencoder", w_errors)

        self.seekable = space.is_true(space.call_method(w_buffer, "seekable"))
        self.telling = self.seekable

        self.encoding_start_of_stream = False
        if self.seekable and self.w_encoder:
            self.encoding_start_of_stream = True
            w_cookie = space.call_method(self.w_buffer, "tell")
            if not space.eq_w(w_cookie, space.newint(0)):
                self.encoding_start_of_stream = False
                space.call_method(self.w_encoder, "setstate", space.newint(0))

        self.state = STATE_OK

    def _check_init(self, space):
        if self.state == STATE_ZERO:
            raise oefmt(space.w_ValueError,
                        "I/O operation on uninitialized object")

    def _check_attached(self, space):
        if self.state == STATE_DETACHED:
            raise oefmt(space.w_ValueError,
                        "underlying buffer has been detached")
        self._check_init(space)

    def _check_closed(self, space, message=None):
        self._check_init(space)
        W_TextIOBase._check_closed(self, space, message)

    def descr_repr(self, space):
        self._check_init(space)
        w_name = space.findattr(self, space.newtext("name"))
        if w_name is None:
            w_name_str = space.newtext("")
        else:
            w_name_str = space.mod(space.newtext("name=%r "), w_name)
        w_args = space.newtuple([w_name_str, self.w_encoding])
        return space.mod(
            space.newtext("<_io.TextIOWrapper %sencoding=%r>"), w_args
        )

    def readable_w(self, space):
        self._check_attached(space)
        return space.call_method(self.w_buffer, "readable")

    def writable_w(self, space):
        self._check_attached(space)
        return space.call_method(self.w_buffer, "writable")

    def seekable_w(self, space):
        self._check_attached(space)
        return space.call_method(self.w_buffer, "seekable")

    def isatty_w(self, space):
        self._check_attached(space)
        return space.call_method(self.w_buffer, "isatty")

    def fileno_w(self, space):
        self._check_attached(space)
        return space.call_method(self.w_buffer, "fileno")

    def closed_get_w(self, space):
        self._check_attached(space)
        return space.getattr(self.w_buffer, space.newtext("closed"))

    def newlines_get_w(self, space):
        self._check_attached(space)
        if self.w_decoder is None:
            return space.w_None
        return space.findattr(self.w_decoder, space.newtext("newlines"))

    def name_get_w(self, space):
        self._check_attached(space)
        return space.getattr(self.w_buffer, space.newtext("name"))

    def flush_w(self, space):
        self._check_attached(space)
        self._check_closed(space)
        self.telling = self.seekable
        self._writeflush(space)
        space.call_method(self.w_buffer, "flush")

    @unwrap_spec(w_pos = WrappedDefault(None))
    def truncate_w(self, space, w_pos=None):
        self._check_attached(space)

        space.call_method(self, "flush")
        return space.call_method(self.w_buffer, "truncate", w_pos)

    def close_w(self, space):
        self._check_attached(space)
        if not space.is_true(space.getattr(self.w_buffer,
                                           space.newtext("closed"))):
            try:
                space.call_method(self, "flush")
            finally:
                ret = space.call_method(self.w_buffer, "close")
            return ret

    # _____________________________________________________________
    # read methods

    def _read_chunk(self, space):
        """Read and decode the next chunk of data from the BufferedReader.
        The return value is True unless EOF was reached.  The decoded string
        is placed in self.decoded (replacing its previous value).
        The entire input chunk is sent to the decoder, though some of it may
        remain buffered in the decoder, yet to be converted."""

        if not self.w_decoder:
            raise oefmt(space.w_IOError, "not readable")

        if self.telling:
            # To prepare for tell(), we need to snapshot a point in the file
            # where the decoder's input buffer is empty.
            w_state = space.call_method(self.w_decoder, "getstate")
            # Given this, we know there was a valid snapshot point
            # len(dec_buffer) bytes ago with decoder state (b'', dec_flags).
            w_dec_buffer, w_dec_flags = space.unpackiterable(w_state, 2)
            dec_buffer = space.bytes_w(w_dec_buffer)
            dec_flags = space.int_w(w_dec_flags)
        else:
            dec_buffer = None
            dec_flags = 0

        # Read a chunk, decode it, and put the result in self.decoded
        w_input = space.call_method(self.w_buffer, "read1",
                                    space.newint(self.chunk_size))

        if not space.isinstance_w(w_input, space.w_bytes):
            msg = "decoder getstate() should have returned a bytes " \
                  "object not '%T'"
            raise oefmt(space.w_TypeError, msg, w_input)

        eof = space.len_w(w_input) == 0
        w_decoded = space.call_method(self.w_decoder, "decode",
                                      w_input, space.newbool(eof))
        self.decoded.set(space, w_decoded)
        if space.len_w(w_decoded) > 0:
            eof = False

        if self.telling:
            # At the snapshot point, len(dec_buffer) bytes before the read,
            # the next input to be decoded is dec_buffer + input_chunk.
            next_input = dec_buffer + space.bytes_w(w_input)
            self.snapshot = PositionSnapshot(dec_flags, next_input)

        return not eof

    def _ensure_data(self, space):
        while not self.decoded.has_data():
            try:
                if not self._read_chunk(space):
                    self.decoded.reset()
                    self.snapshot = None
                    return False
            except OperationError as e:
                if trap_eintr(space, e):
                    continue
                raise
        return True

    def next_w(self, space):
        self._check_attached(space)
        self.telling = False
        try:
            return W_TextIOBase.next_w(self, space)
        except OperationError as e:
            if e.match(space, space.w_StopIteration):
                self.telling = self.seekable
            raise

    def read_w(self, space, w_size=None):
        self._check_attached(space)
        self._check_closed(space)
        if not self.w_decoder:
            raise oefmt(space.w_IOError, "not readable")

        size = convert_size(space, w_size)
        self._writeflush(space)

        if size < 0:
            # Read everything
            w_bytes = space.call_method(self.w_buffer, "read")
            w_decoded = space.call_method(self.w_decoder, "decode", w_bytes, space.w_True)
            check_decoded(space, w_decoded)
            chars = self.decoded.get_chars(-1)
            lgt = get_utf8_length(chars)
            w_result = space.newutf8(chars, lgt)
            w_final = space.add(w_result, w_decoded)
            self.snapshot = None
            return w_final

        remaining = size
        builder = Utf8StringBuilder(size)

        # Keep reading chunks until we have n characters to return
        while remaining > 0:
            if not self._ensure_data(space):
                break
            data = self.decoded.get_chars(remaining)
            builder.append(data)
            remaining -= len(data)

        return space.newutf8(builder.build(), builder.getlength())

    def _scan_line_ending(self, limit):
        if self.readuniversal:
            return self.decoded.find_newline_universal(limit)
        else:
            if self.readtranslate:
                # Newlines are already translated, only search for \n
                newline = '\n'
            else:
                # Non-universal mode.
                newline = self.readnl
            if newline == '\r\n':
                return self.decoded.find_crlf(limit)
            else:
                return self.decoded.find_char(newline[0], limit)

    def readline_w(self, space, w_limit=None):
        self._check_attached(space)
        self._check_closed(space)
        self._writeflush(space)

        limit = convert_size(space, w_limit)
        remnant = None
        builder = StringBuilder()
        # XXX maybe use Utf8StringBuilder instead?
        while True:
            # First, get some data if necessary
            has_data = self._ensure_data(space)
            if not has_data:
                # end of file
                if remnant:
                    builder.append(remnant)
                break

            if remnant:
                assert not self.readtranslate and self.readnl == '\r\n'
                assert self.decoded.pos == 0
                if remnant == '\r' and self.decoded.text[0] == '\n':
                    builder.append('\r\n')
                    self.decoded.pos = 1
                    remnant = None
                    break
                else:
                    builder.append(remnant)
                    remnant = None
                    continue

            if limit >= 0:
                remaining = limit - builder.getlength()
                assert remaining >= 0
            else:
                remaining = -1
            start = self.decoded.pos
            assert start >= 0
            found = self._scan_line_ending(remaining)
            end_scan = self.decoded.pos
            if end_scan > start:
                s = self.decoded.text[start:end_scan]
                builder.append(s)

            if found or (limit >= 0 and builder.getlength() >= limit):
                break

            # There may be some remaining chars we'll have to prepend to the
            # next chunk of data
            if not self.decoded.exhausted():
                remnant = self.decoded.get_chars(-1)
            # We have consumed the buffer
            self.decoded.reset()

        result = builder.build()
        lgt = get_utf8_length(result)
        return space.newutf8(result, lgt)

    # _____________________________________________________________
    # write methods

    def write_w(self, space, w_text):
        self._check_attached(space)
        self._check_closed(space)

        if not self.w_encoder:
            raise oefmt(space.w_IOError, "not writable")

        if not space.isinstance_w(w_text, space.w_unicode):
            raise oefmt(space.w_TypeError,
                        "unicode argument expected, got '%T'", w_text)

        text, textlen = space.utf8_len_w(w_text)

        haslf = False
        if (self.writetranslate and self.writenl) or self.line_buffering:
            if text.find('\n') >= 0:
                haslf = True
        if haslf and self.writetranslate and self.writenl:
            w_text = space.call_method(w_text, "replace", space.newutf8('\n', 1),
                                       space.newutf8(self.writenl, get_utf8_length(self.writenl)))
            text = space.utf8_w(w_text)

        needflush = False
        if self.line_buffering and (haslf or text.find('\r') >= 0):
            needflush = True

        # XXX What if we were just reading?
        if self.encodefunc:
            w_bytes = self.encodefunc(space, w_text, self.errors)
            self.encoding_start_of_stream = False
        else:
            w_bytes = space.call_method(self.w_encoder, "encode", w_text)

        b = space.bytes_w(w_bytes)
        if not self.pending_bytes:
            self.pending_bytes = []
            self.pending_bytes_count = 0
        self.pending_bytes.append(b)
        self.pending_bytes_count += len(b)

        if self.pending_bytes_count > self.chunk_size or needflush:
            self._writeflush(space)

        if needflush:
            space.call_method(self.w_buffer, "flush")

        self.snapshot = None

        if self.w_decoder:
            space.call_method(self.w_decoder, "reset")

        return space.newint(textlen)

    def _writeflush(self, space):
        if not self.pending_bytes:
            return

        pending_bytes = ''.join(self.pending_bytes)
        self.pending_bytes = None
        self.pending_bytes_count = 0

        while True:
            try:
                space.call_method(self.w_buffer, "write",
                                  space.newbytes(pending_bytes))
            except OperationError as e:
                if trap_eintr(space, e):
                    continue
                raise
            else:
                break

    def detach_w(self, space):
        self._check_attached(space)
        space.call_method(self, "flush")
        w_buffer = self.w_buffer
        self.w_buffer = None
        self.state = STATE_DETACHED
        return w_buffer

    # _____________________________________________________________
    # seek/tell

    def _decoder_setstate(self, space, cookie):
        # When seeking to the start of the stream, we call decoder.reset()
        # rather than decoder.getstate().
        # This is for a few decoders such as utf-16 for which the state value
        # at start is not (b"", 0) but e.g. (b"", 2) (meaning, in the case of
        # utf-16, that we are expecting a BOM).
        if cookie.start_pos == 0 and cookie.dec_flags == 0:
            space.call_method(self.w_decoder, "reset")
        else:
            space.call_method(self.w_decoder, "setstate",
                              space.newtuple([space.newbytes(""),
                                              space.newint(cookie.dec_flags)]))

    def _encoder_setstate(self, space, cookie):
        if cookie.start_pos == 0 and cookie.dec_flags == 0:
            space.call_method(self.w_encoder, "reset")
            self.encoding_start_of_stream = True
        else:
            space.call_method(self.w_encoder, "setstate", space.newint(0))
            self.encoding_start_of_stream = False

    @unwrap_spec(whence=int)
    def seek_w(self, space, w_pos, whence=0):
        self._check_attached(space)

        if not self.seekable:
            raise oefmt(space.w_IOError, "underlying stream is not seekable")

        if whence == 1:
            # seek relative to current position
            if not space.eq_w(w_pos, space.newint(0)):
                raise oefmt(space.w_IOError,
                            "can't do nonzero cur-relative seeks")
            # Seeking to the current position should attempt to sync the
            # underlying buffer with the current position.
            w_pos = space.call_method(self, "tell")

        elif whence == 2:
            # seek relative to end of file
            if not space.eq_w(w_pos, space.newint(0)):
                raise oefmt(space.w_IOError,
                            "can't do nonzero end-relative seeks")
            space.call_method(self, "flush")
            self.decoded.reset()
            self.snapshot = None
            if self.w_decoder:
                space.call_method(self.w_decoder, "reset")
            return space.call_method(self.w_buffer, "seek",
                                     w_pos, space.newint(whence))

        elif whence != 0:
            raise oefmt(space.w_ValueError,
                        "invalid whence (%d, should be 0, 1 or 2)",
                        whence)

        if space.is_true(space.lt(w_pos, space.newint(0))):
            raise oefmt(space.w_ValueError,
                        "negative seek position %R", w_pos)

        space.call_method(self, "flush")

        # The strategy of seek() is to go back to the safe start point and
        # replay the effect of read(chars_to_skip) from there.
        cookie = PositionCookie(space.bigint_w(w_pos))

        # Seek back to the safe start point
        space.call_method(self.w_buffer, "seek", space.newint(cookie.start_pos))

        self.decoded.reset()
        self.snapshot = None

        # Restore the decoder to its state from the safe start point.
        if self.w_decoder:
            self._decoder_setstate(space, cookie)

        if cookie.chars_to_skip:
            # Just like _read_chunk, feed the decoder and save a snapshot.
            w_chunk = space.call_method(self.w_buffer, "read",
                                        space.newint(cookie.bytes_to_feed))
            if not space.isinstance_w(w_chunk, space.w_bytes):
                msg = "underlying read() should have returned " \
                      "a bytes object, not '%T'"
                raise oefmt(space.w_TypeError, msg, w_chunk)

            self.snapshot = PositionSnapshot(cookie.dec_flags,
                                             space.bytes_w(w_chunk))

            w_decoded = space.call_method(self.w_decoder, "decode",
                                          w_chunk, space.newbool(bool(cookie.need_eof)))
            w_decoded = check_decoded(space, w_decoded)

            # Skip chars_to_skip of the decoded characters
            if space.len_w(w_decoded) < cookie.chars_to_skip:
                raise oefmt(space.w_IOError,
                            "can't restore logical file position")
            self.decoded.set(space, w_decoded)
            self.decoded.pos = w_decoded._index_to_byte(cookie.chars_to_skip)
        else:
            self.snapshot = PositionSnapshot(cookie.dec_flags, "")

        # Finally, reset the encoder (merely useful for proper BOM handling)
        if self.w_encoder:
            self._encoder_setstate(space, cookie)

        return w_pos

    def tell_w(self, space):
        self._check_closed(space)
        if not self.seekable:
            raise oefmt(space.w_IOError, "underlying stream is not seekable")
        if not self.telling:
            raise oefmt(space.w_IOError,
                        "telling position disabled by next() call")

        self._writeflush(space)
        space.call_method(self, "flush")

        w_pos = space.call_method(self.w_buffer, "tell")

        if self.w_decoder is None or self.snapshot is None:
            assert not self.decoded.text
            return w_pos

        cookie = PositionCookie(space.bigint_w(w_pos))

        # Skip backward to the snapshot point (see _read_chunk)
        cookie.dec_flags = self.snapshot.flags
        input = self.snapshot.input
        cookie.start_pos -= len(input)

        # How many decoded characters have been used up since the snapshot?
        if not self.decoded.pos:
            # We haven't moved from the snapshot point.
            return space.newlong_from_rbigint(cookie.pack())

        chars_to_skip = codepoints_in_utf8(
            self.decoded.text, end=self.decoded.pos)

        # Starting from the snapshot position, we will walk the decoder
        # forward until it gives us enough decoded characters.
        w_saved_state = space.call_method(self.w_decoder, "getstate")

        try:
            # Note our initial start point
            self._decoder_setstate(space, cookie)

            # Feed the decoder one byte at a time.  As we go, note the nearest
            # "safe start point" before the current location (a point where
            # the decoder has nothing buffered, so seek() can safely start
            # from there and advance to this location).

            chars_decoded = 0
            i = 0
            while i < len(input):
                w_decoded = space.call_method(self.w_decoder, "decode",
                                              space.newbytes(input[i]))
                check_decoded(space, w_decoded)
                chars_decoded += space.len_w(w_decoded)

                cookie.bytes_to_feed += 1

                w_state = space.call_method(self.w_decoder, "getstate")
                w_dec_buffer, w_flags = space.unpackiterable(w_state, 2)
                dec_buffer_len = space.len_w(w_dec_buffer)

                if dec_buffer_len == 0 and chars_decoded <= chars_to_skip:
                    # Decoder buffer is empty, so this is a safe start point.
                    cookie.start_pos += cookie.bytes_to_feed
                    chars_to_skip -= chars_decoded
                    assert chars_to_skip >= 0
                    cookie.dec_flags = space.int_w(w_flags)
                    cookie.bytes_to_feed = 0
                    chars_decoded = 0
                if chars_decoded >= chars_to_skip:
                    break
                i += 1
            else:
                # We didn't get enough decoded data; signal EOF to get more.
                w_decoded = space.call_method(self.w_decoder, "decode",
                                              space.newbytes(""),
                                              space.newint(1))  # final=1
                check_decoded(space, w_decoded)
                chars_decoded += space.len_w(w_decoded)
                cookie.need_eof = 1

                if chars_decoded < chars_to_skip:
                    raise oefmt(space.w_IOError,
                        "can't reconstruct logical file position")
        finally:
            space.call_method(self.w_decoder, "setstate", w_saved_state)

        # The returned cookie corresponds to the last safe start point.
        cookie.chars_to_skip = chars_to_skip
        return space.newlong_from_rbigint(cookie.pack())

    def chunk_size_get_w(self, space):
        self._check_attached(space)
        return space.newint(self.chunk_size)

    def chunk_size_set_w(self, space, w_size):
        self._check_attached(space)
        size = space.int_w(w_size)
        if size <= 0:
            raise oefmt(space.w_ValueError,
                        "a strictly positive integer is required")
        self.chunk_size = size

W_TextIOWrapper.typedef = TypeDef(
    '_io.TextIOWrapper', W_TextIOBase.typedef,
    __new__ = generic_new_descr(W_TextIOWrapper),
    __init__  = interp2app(W_TextIOWrapper.descr_init),
    __repr__ = interp2app(W_TextIOWrapper.descr_repr),

    next = interp2app(W_TextIOWrapper.next_w),
    read = interp2app(W_TextIOWrapper.read_w),
    readline = interp2app(W_TextIOWrapper.readline_w),
    write = interp2app(W_TextIOWrapper.write_w),
    seek = interp2app(W_TextIOWrapper.seek_w),
    tell = interp2app(W_TextIOWrapper.tell_w),
    detach = interp2app(W_TextIOWrapper.detach_w),
    flush = interp2app(W_TextIOWrapper.flush_w),
    truncate = interp2app(W_TextIOWrapper.truncate_w),
    close = interp2app(W_TextIOWrapper.close_w),

    line_buffering = interp_attrproperty("line_buffering", W_TextIOWrapper,
        wrapfn="newint"),
    readable = interp2app(W_TextIOWrapper.readable_w),
    writable = interp2app(W_TextIOWrapper.writable_w),
    seekable = interp2app(W_TextIOWrapper.seekable_w),
    isatty = interp2app(W_TextIOWrapper.isatty_w),
    fileno = interp2app(W_TextIOWrapper.fileno_w),
    name = GetSetProperty(W_TextIOWrapper.name_get_w),
    buffer = interp_attrproperty_w("w_buffer", cls=W_TextIOWrapper),
    closed = GetSetProperty(W_TextIOWrapper.closed_get_w),
    errors = interp_attrproperty_w("w_errors", cls=W_TextIOWrapper),
    newlines = GetSetProperty(W_TextIOWrapper.newlines_get_w),
    _CHUNK_SIZE = GetSetProperty(
        W_TextIOWrapper.chunk_size_get_w, W_TextIOWrapper.chunk_size_set_w
    ),
)
