#!/usr/bin/env python3
import asyncio
import socket
import sys
from enum import Enum
from uap_header import * # Assumes uap_header.py is in the same directory

class ClientState(Enum):
    """Enumeration for the client's state machine."""
    START = "START"
    WAIT_HELLO = "WAIT_HELLO"
    ESTABLISHED = "ESTABLISHED"
    WAIT_ALIVE = "WAIT_ALIVE"
    WAIT_GOODBYE = "WAIT_GOODBYE"
    CLOSED = "CLOSED"

class UAPClientProtocol(asyncio.DatagramProtocol):
    """
    Implements the UAP client logic as an asyncio DatagramProtocol.
    This class manages the client's state machine, message sending/receiving,
    and timeout handling.
    """
    def __init__(self, loop, on_con_lost):
        self.loop = loop
        self.on_con_lost = on_con_lost
        self.transport = None
        self.state = ClientState.START
        
        self.session_id = generate_session_id()
        self.sequence_number = 0
        self.logical_clock = 0
        
        self.timeout_handle = None
        # This event is used to signal the stdin reader when it's okay to send data.
        self.can_send_event = asyncio.Event()

    def connection_made(self, transport):
        """Called by asyncio when the transport is ready."""
        self.transport = transport
        peername = transport.get_extra_info('peername')
        print(f"Connecting to {peername} with session 0x{self.session_id:08x}")
        self.send_hello()

    def datagram_received(self, data, addr):
        """Called by asyncio when a UDP datagram is received."""
        # A response was received, so cancel the pending timeout.
        if self.timeout_handle:
            self.timeout_handle.cancel()
            self.timeout_handle = None
        
        try:
            header = unpack_header(data)
            
            # Silently discard packets that don't match our session context.
            if header["magic"] != Constants["magic"] or \
               header["version"] != Constants["version"] or \
               header["session_id"] != self.session_id:
                return
            
            # Update logical clock based on the Lamport algorithm.
            self.logical_clock = update_logical_clock(self.logical_clock, header["logical_clock"])
            
            command = header["command"]
            
            # A GOODBYE from the server terminates the session immediately.
            if command == Constants["command_GOODBYE"]:
                self.close_connection("Received GOODBYE from server.")
                return

            # --- FSA State Logic ---
            if self.state == ClientState.WAIT_HELLO and command == Constants["command_HELLO"]:
                print("Session established.")
                self.state = ClientState.ESTABLISHED
                self.can_send_event.set()  # Signal stdin reader to start.
            
            elif self.state == ClientState.WAIT_ALIVE and command == Constants["command_ALIVE"]:
                self.state = ClientState.ESTABLISHED
                self.can_send_event.set()  # Signal stdin reader for the next line.
                
            elif self.state == ClientState.WAIT_GOODBYE:
                # GOODBYE was already handled, just close cleanly.
                self.close_connection("GOODBYE acknowledged by server.")
                
            else:
                self.close_connection(f"Protocol error: Received command {command} in state {self.state.name}")
                
        except Exception as e:
            self.close_connection(f"Packet processing error: {e}")

    def error_received(self, exc):
        """Called by asyncio when a send/receive operation raises an error."""
        self.close_connection(f"Socket error: {exc}")

    def connection_lost(self, exc):
        """Called by asyncio when the connection is closed."""
        if not self.on_con_lost.done():
            self.on_con_lost.set_result(True)

    def _send_message(self, message):
        """Internal helper to send a message and schedule a timeout for the response."""
        self.transport.sendto(message)
        self.timeout_handle = self.loop.call_later(RTO_MS / 1000.0, self.handle_timeout)
        
    def send_hello(self):
        """Sends a HELLO message and transitions to WAIT_HELLO state."""
        self.logical_clock += 1
        hello_msg = create_hello_message(self.sequence_number, self.session_id, self.logical_clock)
        self.sequence_number += 1
        self.state = ClientState.WAIT_HELLO
        self._send_message(hello_msg)

    def send_data(self, line):
        """Sends a DATA message and transitions to WAIT_ALIVE state."""
        self.can_send_event.clear()  # Block stdin until we get a response.
        self.logical_clock += 1
        data_msg = create_data_message(self.sequence_number, self.session_id, self.logical_clock, line)
        self.sequence_number += 1
        self.state = ClientState.WAIT_ALIVE
        self._send_message(data_msg)

    def send_goodbye(self):
        """Sends a GOODBYE message and transitions to WAIT_GOODBYE state."""
        self.can_send_event.clear()
        self.logical_clock += 1
        goodbye_msg = create_goodbye_message(self.sequence_number, self.session_id, self.logical_clock)
        self.sequence_number += 1
        self.state = ClientState.WAIT_GOODBYE
        self._send_message(goodbye_msg)

    def handle_timeout(self):
        """Callback for when a response is not received within the RTO."""
        self.close_connection("Timeout: No response from server.")

    def close_connection(self, reason=""):
        """Shuts down the client connection gracefully."""
        if self.state != ClientState.CLOSED:
            print(f"Closing connection: {reason}")
            self.state = ClientState.CLOSED
            if self.timeout_handle:
                self.timeout_handle.cancel()
            self.can_send_event.set()  # Unblock stdin reader if it's waiting.
            self.transport.close()


async def read_stdin_and_send(protocol: UAPClientProtocol, loop):
    """
    Asynchronous task to read lines from stdin and pass them to the protocol
    for sending. This task coordinates with the protocol using an asyncio.Event
    to ensure messages are sent one at a time, pending a response.
    """
    # Wait for the initial HELLO handshake to complete.
    await protocol.can_send_event.wait()

    while protocol.state not in [ClientState.CLOSED, ClientState.WAIT_GOODBYE]:
        try:
            # Read the next line from stdin in a non-blocking way.
            line = await loop.run_in_executor(None, sys.stdin.readline)
            
            if not line:  # EOF detected
                print("eof")
                break
                
            line = line.strip()
            # In an interactive terminal, 'q' also quits.
            if sys.stdin.isatty() and line == 'q':
                break

            # Wait until the protocol is in the ESTABLISHED state to send.
            await protocol.can_send_event.wait()
            if protocol.state == ClientState.ESTABLISHED:
                protocol.send_data(line)
            else:
                # Protocol closed while we were reading a line.
                break

        except Exception as e:
            print(f"Stdin reader error: {e}")
            break
            
    # If the loop breaks, initiate the shutdown sequence if it's not already happening.
    if protocol.state not in [ClientState.CLOSED, ClientState.WAIT_GOODBYE]:
        protocol.send_goodbye()


async def main():
    """Main entry point for the client application."""
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <hostname> <portnum>", file=sys.stderr)
        sys.exit(1)

    hostname = sys.argv[1]
    try:
        port = int(sys.argv[2])
    except ValueError:
        print("Error: Port must be a number", file=sys.stderr)
        sys.exit(1)

    loop = asyncio.get_running_loop()
    # A Future to signal when the connection is lost.
    on_con_lost = loop.create_future()

    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: UAPClientProtocol(loop, on_con_lost),
            remote_addr=(hostname, port)
        )

        # Start the task that reads from stdin.
        stdin_task = loop.create_task(read_stdin_and_send(protocol, loop))

        # Wait until the protocol signals that the connection is closed.
        await on_con_lost
        
        # Clean up the stdin task.
        stdin_task.cancel()
        try:
            await stdin_task
        except asyncio.CancelledError:
            pass  # This is expected on cancellation.

    except socket.gaierror:
        print(f"Error: Hostname '{hostname}' could not be resolved.", file=sys.stderr)
    except OSError as e:
        print(f"Error: Could not connect to {hostname}:{port}. {e}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
    finally:
        if 'transport' in locals() and not transport.is_closing():
            transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nClient interrupted by user.")