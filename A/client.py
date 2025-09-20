#!/usr/bin/env python3

import socket
import sys
import time
import select
from uap_header import *

class BasicClient:
    def __init__(self, hostname, port):
        self.hostname = hostname
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.session_id = generate_session_id()
        self.sequence_number = 0
        self.logical_clock = 0
        self.state = 'START'
        self.server_addr = (hostname, port)

    def run(self):
        """Main client loop"""
        try:
            # print(f"DEBUG: Starting client, session_id = 0x{self.session_id:08x}")
            # Start session with HELLO (with retransmits)
            if not self.send_hello_with_retries():
                print("No response from server (HELLO retries exhausted)")
                return
            # print("DEBUG: HELLO successful, connected to server")
            self.state = 'CONNECTED'

            # Check if stdin is a tty
            stdin_is_tty = sys.stdin.isatty()
            # print(f"DEBUG: stdin_is_tty = {stdin_is_tty}")

            while self.state != 'CLOSED':
                if self.state == 'CONNECTED':
                    self.handle_input(stdin_is_tty)

        except KeyboardInterrupt:
            print("\nClient shutting down...")
        finally:
            self.cleanup()

    def send_hello(self):
        """Send HELLO message to server"""
        self.logical_clock += 1
        message = create_hello_message(self.sequence_number, self.session_id, self.logical_clock)
        # print(f"DEBUG: Sending HELLO, seq={self.sequence_number}, session=0x{self.session_id:08x}")
        self.socket.sendto(message, self.server_addr)
        self.sequence_number += 1

    def send_data(self, data):
        """Send DATA message to server"""
        self.logical_clock += 1
        message = create_data_message(self.sequence_number, self.session_id, self.logical_clock, data)
        # print(f"DEBUG: Sending DATA, seq={self.sequence_number}, data='{data}'")
        self.socket.sendto(message, self.server_addr)
        self.sequence_number += 1

    def send_goodbye(self):
        """Send GOODBYE message to server"""
        self.logical_clock += 1
        message = create_goodbye_message(self.sequence_number, self.session_id, self.logical_clock)
        # print(f"DEBUG: Sending GOODBYE, seq={self.sequence_number}")
        self.socket.sendto(message, self.server_addr)
        self.sequence_number += 1
        self.state = 'WAIT_GOODBYE'

    def wait_for_response(self, timeout=RTO_MS/1000.0):
        """Wait for a single response from the server (within timeout)."""
        # print(f"DEBUG: Waiting for response, timeout={timeout}s")
        ready = select.select([self.socket], [], [], timeout)
        if not ready[0]:
            # print("DEBUG: Timeout waiting for response")
            return None, None
        try:
            packet, addr = self.socket.recvfrom(4096)
            # print(f"DEBUG: Received packet from {addr}, length={len(packet)}")
            # Parse header and message safely
            header = unpack_header(packet)
            # print(f"DEBUG: Parsed header: session=0x{header['session_id']:08x}, cmd={header['command']}, seq={header['sequence_number']}")
            # Only accept responses for our session
            if header.get('session_id') != self.session_id:
                # print(f"DEBUG: Ignoring packet for different session 0x{header['session_id']:08x}")
                return None, None
            message = parse_message(packet)
            # Update logical clock according to Lamport rule
            self.logical_clock = update_logical_clock(self.logical_clock, message.logical_clock)
            # print(f"DEBUG: Valid response received, cmd={header['command']}")
            return message, header
        except Exception as e:
            print(f"DEBUG: Error parsing response: {e}")
            return None, None

    def send_with_retries(self, build_message_fn, expected_commands):
        """Helper to send a message and wait for response with retries."""
        for attempt in range(MAX_RETRIES):
            # print(f"DEBUG: Retry attempt {attempt + 1}/{MAX_RETRIES}")
            # build and send message
            build_message_fn()
            # wait for response
            msg, hdr = self.wait_for_response()
            if msg is None:
                # print(f"DEBUG: No response on attempt {attempt + 1}")
                continue
            cmd = hdr.get('command')
            if cmd in expected_commands:
                # print(f"DEBUG: Got expected response cmd={cmd}")
                return msg, hdr
            else:
                print(f"DEBUG: Got unexpected response cmd={cmd}, expected one of {expected_commands}")
        print(f"DEBUG: All {MAX_RETRIES} attempts failed")
        return None, None

    def send_hello_with_retries(self):
        # expect server to reply with HELLO
        msg, hdr = self.send_with_retries(self.send_hello, [Constants["command_HELLO"]])
        return msg is not None

    def send_data_with_retries(self, data):
        # expect ALIVE
        def _send():
            self.send_data(data)
        msg, hdr = self.send_with_retries(_send, [Constants["command_ALIVE"]])
        return msg is not None

    def send_goodbye_with_retries(self):
        # expect GOODBYE
        def _send():
            self.send_goodbye()
        msg, hdr = self.send_with_retries(_send, [Constants["command_GOODBYE"]])
        return msg is not None

    def handle_input(self, stdin_is_tty):
        """Handle user input"""
        try:
            # Check if there's input available
            ready = select.select([sys.stdin], [], [], 0.1)
            if ready[0]:
                line = sys.stdin.readline()
                if not line:  # EOF
                    if stdin_is_tty:
                        print("eof")
                    # send goodbye and wait for server GOODBYE (with retries)
                    if self.send_goodbye_with_retries():
                        self.state = 'CLOSED'
                    else:
                        self.state = 'CLOSED'
                    return

                line = line.strip()
                if line == 'q' and stdin_is_tty:
                    if self.send_goodbye_with_retries():
                        self.state = 'CLOSED'
                    else:
                        self.state = 'CLOSED'
                    return

                # Send data and wait for ALIVE (with retransmits)
                # print(f"DEBUG: About to send data: '{line}'")
                success = self.send_data_with_retries(line)
                if not success:
                    print("No ALIVE from server (data retransmit retries exhausted).")
                return

        except Exception as e:
            print(f"Error handling input: {e}")

    def cleanup(self):
        """Cleanup resources"""
        if self.state not in ['CLOSED', 'WAIT_GOODBYE']:
            try:
                self.send_goodbye()
            except Exception:
                pass
        try:
            self.socket.close()
        except Exception:
            pass

def main():
    if len(sys.argv) != 3:
        print("Usage: ./client <hostname> <portnum>")
        sys.exit(1)

    try:
        hostname = sys.argv[1]
        port = int(sys.argv[2])
        client = BasicClient(hostname, port)
        client.run()
    except ValueError:
        print("Error: Port must be a number")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()