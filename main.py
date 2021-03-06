#!/usr/bin/env python3
import click
import sys
import threading
import select
import pty
import os
import re
import requests
import time
import logging
import codecs
from matrix_client.client import MatrixClient


SHELL_CMD_REGEX = r'!shell (.*)'
CTRLC_CMD_REGEX = r'!ctrl\+c|!ctrlc|!shell ctrlc|!shell ctrl\+c'
MAX_STDOUT_PER_MSG = 1024 * 16

logger = logging.getLogger('shellbot')
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s")
escape_parser = re.compile(r'\x1b\[?([\d;]*)(\w)')
cleared_line_parser = re.compile(r'[^\n]*\x1b\[K')


def handle_escape_codes(shell_out):
    """
    Parses (as best we can) the raw escape codes in a bunch of shell output
    and converts it into chatclient-friendly text.
    shell_out: string of shell output
    """
    shell_out_after_clears = re.sub(cleared_line_parser, "", shell_out)
    shell_out_noescapes = re.sub(escape_parser, "", shell_out_after_clears)
    return shell_out_noescapes


def on_message(event, pin, allowed_users):
    """
    Writes contents of a message event to the shell.

    event: matrix event dict
    pin: file object for pty master
    allowed_users: users authorized to send input to the shell

    A newline is appended to the text contents when written, so a one-line
    message may be interpreted as a command.

    Special cases: !ctrlc sends a sequence as if the user typed ctrl+c.
    """
    if event['sender'] in allowed_users and (
            'msgtype' in event['content'] and
            event['content']['msgtype'] == 'm.text'):
        message = str(event['content']['body'])
        if re.match(CTRLC_CMD_REGEX, message, flags=re.I):
            logger.info('sending ctrl+c')
            pin.write('\x03')
            pin.flush()
        else:
            cmd_match = re.match(SHELL_CMD_REGEX, message, flags=re.I)
            if cmd_match:
                message = cmd_match.group(1)
                logger.info('shell stdin: {}'.format(message))
                pin.write(message)
                pin.write('\n')
                pin.flush()


def get_inviter(invite_state, user_id):
    for event in invite_state['events']:
        logger.info(event)
        if event['type'] == 'm.room.member' and (
                event['content']['membership'] == 'invite' and
                event['state_key'] == user_id):
            return event['sender']


def on_invite(client, room_id, state, allowed_users):
    inviter = get_inviter(state, client.user_id)
    if inviter in allowed_users:
        logger.info("joining room {} from {}'s invitation"
                    .format(room_id, inviter))
        client.join_room(room_id)


def stdout_to_messages(buf, incremental_decoder, flush=True):
    """
    Returns a list of strings to be sent in separate matrix messages.
    Mutates buf to include only unsent shell output.

    If flush is True, we output everything we have.
    Otherwise, we only output if we reach our maximum message size, in which
    case we split output into multiple messages, doing our best to split on
    newlines.

    Note that some intermediate output will be kept by the incremental decoder.
    This is needed so we don't accidentally cut off output in the middle of a
    utf8 multibyte character.

    buf: list of bytestrings read from shell process, each <=1024 bytes long
    incremental_decoder: incremental utf8 decoder
    flush: whether to output all text, even if we haven't reached the message
           size limit yet
    """
    if flush:
        total_stdout = b''.join(buf)
        buf.clear()
        return [incremental_decoder.decode(total_stdout)]
    elif sum(len(s) for s in buf) > MAX_STDOUT_PER_MSG:
        # grab <=1k chunks of stdout until we run out or have nearly too much
        stdout_to_send = []
        bytes_to_send = 0
        while buf and len(buf[0]) + bytes_to_send <= MAX_STDOUT_PER_MSG:
            stdout_to_send.append(buf.pop(0))
            bytes_to_send += len(stdout_to_send[-1])
        total_stdout = b''.join(stdout_to_send)
        # cut off everything until the last newline, if any
        last_newline = total_stdout.rfind(b'\n')
        if last_newline != -1:
            buf.append(total_stdout[last_newline + 1:])
            return [incremental_decoder.decode(total_stdout[:last_newline])]
        else:
            # it's all one huge line. send it anyways.
            return [incremental_decoder.decode(total_stdout)]
    return []


def shell_stdout_handler(master, client, stop):
    """
    Reads output from the shell process until there's a 0.1s+ period of no
    output. Then, sends it as a message to all allowed matrix rooms.

    master: master pipe for the pty. gives us read/write with the shell.
    client: matrix client
    stop: threading.Event that activates when the bot shuts down

    This function exits when stop is set.
    """
    buf = []
    decoder = codecs.getincrementaldecoder('utf8')(errors='replace')
    while not stop.is_set():
        shell_has_more = select.select([master], [], [], 0.1)[0]
        if shell_has_more:
            shell_stdout = os.read(master, 1024)
            if shell_stdout == '':
                return
            buf.append(shell_stdout)
        if buf and client.rooms:
            for shell_out in stdout_to_messages(
                    buf, decoder, flush=not shell_has_more):
                logger.info('shell stdout: {}'.format(shell_out))
                text = handle_escape_codes(shell_out)
                text = text.replace('\r', '')
                html = '<pre><code>' + text + '</code></pre>'
                for room in client.rooms.values():
                    room.send_html(html, body=text)


@click.command()
@click.option('--homeserver', default='https://matrix.org',
              help='matrix homeserver url')
@click.option('--authorize', default=['@matthew:vgd.me'], multiple=True,
              help='authorize user to issue commands '
              '& invite the bot to rooms')
@click.argument('username')
@click.argument('password')
def run_bot(homeserver, authorize, username, password):
    allowed_users = authorize
    shell_env = os.environ.copy()
    shell_env['TERM'] = 'vt100'
    child_pid, master = pty.fork()
    if child_pid == 0:  # we are the child
        os.execlpe('sh', 'sh', shell_env)
    pin = os.fdopen(master, 'w')
    stop = threading.Event()

    client = MatrixClient(homeserver)
    client.login_with_password_no_sync(username, password)
    # listen for invites during initial event sync so we don't miss any
    client.add_invite_listener(
        lambda room_id, state: on_invite(client, room_id, state,
                                         allowed_users))
    client.listen_for_events()  # get rid of initial event sync
    client.add_listener(lambda event: on_message(event, pin, allowed_users),
                        event_type='m.room.message')

    shell_stdout_handler_thread = threading.Thread(
        target=shell_stdout_handler, args=(master, client, stop))
    shell_stdout_handler_thread.start()

    while True:
        try:
            client.listen_forever()
        except KeyboardInterrupt:
            stop.set()
            sys.exit(0)
        except requests.exceptions.Timeout:
            logger.warn("timeout. Trying again in 5s...")
            time.sleep(5)
        except requests.exceptions.ConnectionError as e:
            logger.warn(repr(e))
            logger.warn("disconnected. Trying again in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    run_bot(auto_envvar_prefix='SHELLBOT')
