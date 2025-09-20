#!/usr/bin/env python3
import asyncio
import socket
import sys
import time
from enum import Enum
from uap_header import *

class SessionState(Enum):
    START = "START"
    RECEIVE = "RECEIVE" 
    DONE = "DONE"

class Session:
    def __init__(self, session_id, server_socket, client_address, server):
        self.session_id = session_id
        self.state = SessionState.START
        self.server_socket = server_socket
        self.client_address = client_address
        self.server = server
        self.expected_sequence = 0
        self.server_sequence = 0
        self.logical_clock = 0
        self.timer_task = None
        
    def set_inactivity_timer(self):
        """Set or reset the inactivity timer"""
        if self.timer_task:
            self.timer_task.cancel()
        self.timer_task = asyncio.create_task(self.inactivity_timeout())
    
    async def inactivity_timeout(self):
        """Handle inactivity timeout"""
        try:
            await asyncio.sleep(INACTIVITY_MS / 1000.0)  # Convert to seconds
            print(f"0x{self.session_id:08x} Session timed out")
            await self.close_session()
        except asyncio.CancelledError:
            pass
    
    async def close_session(self):
        """Close the session and send GOODBYE"""
        if self.state != SessionState.DONE:
            self.logical_clock += 1
            goodbye_msg = create_goodbye_message(
                self.server_sequence, self.session_id, self.logical_clock
            )
            self.server_sequence += 1
            
            try:
                self.server_socket.sendto(goodbye_msg, self.client_address)
            except Exception as e:
                print(f"Error sending GOODBYE: {e}")
            
            self.state = SessionState.DONE
            if self.timer_task:
                self.timer_task.cancel()
            
            print(f"0x{self.session_id:08x} Session closed")
            self.server.remove_session(self.session_id)
    
    async def handle_message(self, packet, addr):
        """Handle incoming message based on current state"""
        try:
            header = unpack_header(packet)
            
            # Validate magic and version
            if header["magic"] != Constants["magic"] or header["version"] != Constants["version"]:
                return  # Silently discard
            
            command = header["command"]
            sequence_number = header["sequence_number"]
            received_clock = header["logical_clock"]
            timestamp = header["timestamp"]
            
            # Update logical clock
            self.logical_clock = update_logical_clock(self.logical_clock, received_clock)
            
            # Calculate one-way latency
            current_time = curr_time_stamp()
            latency_us = current_time - timestamp
            latency_ms = latency_us / 1000.0
            
            if self.state == SessionState.START:
                await self.handle_start_state(command, sequence_number, packet)
            elif self.state == SessionState.RECEIVE:
                await self.handle_receive_state(command, sequence_number, packet, latency_ms)
            else:  # DONE state
                # No transitions from DONE state, ignore all messages
                pass
                
        except Exception as e:
            print(f"Error handling message: {e}")
            await self.close_session()
    
    async def handle_start_state(self, command, sequence_number, packet):
        """Handle messages in START state"""
        if command == Constants["command_HELLO"]:
            if sequence_number != 0:
                print(f"0x{self.session_id:08x} Protocol error: HELLO with wrong sequence number")
                await self.close_session()
                return
            
            print(f"0x{self.session_id:08x} [0] Session created")
            
            # Send HELLO response
            self.logical_clock += 1
            hello_response = create_hello_message(
                self.server_sequence, self.session_id, self.logical_clock
            )
            self.server_sequence += 1
            
            try:
                self.server_socket.sendto(hello_response, self.client_address)
            except Exception as e:
                print(f"Error sending HELLO response: {e}")
                await self.close_session()
                return
            
            # Set timer and transition to RECEIVE
            self.set_inactivity_timer()
            self.expected_sequence = 1
            self.state = SessionState.RECEIVE
            
        else:
            # Protocol error: first message must be HELLO
            print(f"0x{self.session_id:08x} Protocol error: Expected HELLO, got {command}")
            await self.close_session()
    
    async def handle_receive_state(self, command, sequence_number, packet, latency_ms):
        """Handle messages in RECEIVE state"""
        if command == Constants["command_DATA"]:
            await self.handle_data_message(sequence_number, packet, latency_ms)
        elif command == Constants["command_GOODBYE"]:
            print(f"0x{self.session_id:08x} [{sequence_number}] GOODBYE from client.")
            await self.close_session()
        elif command == Constants["command_HELLO"]:
            # Protocol error: HELLO in RECEIVE state
            print(f"0x{self.session_id:08x} Protocol error: Unexpected HELLO in RECEIVE state")
            await self.close_session()
        else:
            # Unknown command or ALIVE (which client shouldn't send)
            print(f"0x{self.session_id:08x} Protocol error: Unexpected command {command}")
            await self.close_session()
    
    async def handle_data_message(self, sequence_number, packet, latency_ms):
        """Handle DATA message with sequence number checking"""
        if sequence_number == self.expected_sequence:
            # Expected sequence number
            data = extract_data(packet)
            print(f"0x{self.session_id:08x} [{sequence_number}] {data}")
            
            # Send ALIVE response
            self.logical_clock += 1
            alive_msg = create_alive_message(
                self.server_sequence, self.session_id, self.logical_clock
            )
            self.server_sequence += 1
            
            try:
                self.server_socket.sendto(alive_msg, self.client_address)
                print(f"One-way latency: {latency_ms:.2f} ms")
            except Exception as e:
                print(f"Error sending ALIVE: {e}")
                await self.close_session()
                return
            
            self.expected_sequence += 1
            self.set_inactivity_timer()  # Reset timer
            
        elif sequence_number > self.expected_sequence:
            # Lost packets
            for lost_seq in range(self.expected_sequence, sequence_number):
                print(f"0x{self.session_id:08x} [{lost_seq}] Lost packet!")
            
            # Process current packet
            data = extract_data(packet)
            print(f"0x{self.session_id:08x} [{sequence_number}] {data}")
            
            # Send ALIVE response
            self.logical_clock += 1
            alive_msg = create_alive_message(
                self.server_sequence, self.session_id, self.logical_clock
            )
            self.server_sequence += 1
            
            try:
                self.server_socket.sendto(alive_msg, self.client_address)
                print(f"One-way latency: {latency_ms:.2f} ms")
            except Exception as e:
                print(f"Error sending ALIVE: {e}")
                await self.close_session()
                return
            
            self.expected_sequence = sequence_number + 1
            self.set_inactivity_timer()  # Reset timer
            
        elif sequence_number == self.expected_sequence - 1:
            # Duplicate packet
            print(f"0x{self.session_id:08x} [{sequence_number}] Duplicate packet!")
            # Discard packet, don't respond
            
        else:
            # Sequence number from the past (protocol error)
            print(f"0x{self.session_id:08x} Protocol error: Sequence number from past")
            await self.close_session()

class UAP_Server:
    def __init__(self, port):
        self.port = port
        self.sessions = {}
        self.socket = None
        self.running = False
        
    def remove_session(self, session_id):
        """Remove session from active sessions"""
        if session_id in self.sessions:
            del self.sessions[session_id]
    
    async def handle_stdin(self):
        """Handle stdin input in a separate task"""
        loop = asyncio.get_event_loop()
        
        while self.running:
            try:
                # Read from stdin asynchronously
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:  # EOF
                    break
                line = line.strip()
                if line == 'q':
                    break
            except Exception:
                break
        
        # Shutdown server
        await self.shutdown()
    
    async def shutdown(self):
        """Shutdown server and close all sessions"""
        self.running = False
        
        # Send GOODBYE to all active sessions
        for session in list(self.sessions.values()):
            await session.close_session()
        
        if self.socket:
            self.socket.close()
    
    async def run(self):
        """Main server loop"""
        # Create UDP socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.socket.bind(('', self.port))
            self.socket.setblocking(False)  # Non-blocking socket
            print(f"Waiting on port {self.port}...")
            self.running = True
            
            # Start stdin handler
            stdin_task = asyncio.create_task(self.handle_stdin())
            
            # Main event loop
            while self.running:
                try:
                    # Wait for network data
                    data, addr = await asyncio.get_event_loop().sock_recvfrom(
                        self.socket, 1024
                    )
                    
                    if data:
                        await self.handle_packet(data, addr)
                        
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    if self.running:
                        print(f"Error receiving packet: {e}")
            
            # Cancel stdin task
            stdin_task.cancel()
            try:
                await stdin_task
            except asyncio.CancelledError:
                pass
                
        except Exception as e:
            print(f"Server error: {e}")
        finally:
            if self.socket:
                self.socket.close()
    
    async def handle_packet(self, packet, addr):
        """Handle incoming packet"""
        try:
            # Parse header to get session ID
            header = unpack_header(packet)
            
            # Check magic and version
            if header["magic"] != Constants["magic"] or header["version"] != Constants["version"]:
                return  # Silently discard
            
            session_id = header["session_id"]
            
            # Find or create session
            if session_id not in self.sessions:
                # Create new session
                session = Session(session_id, self.socket, addr, self)
                self.sessions[session_id] = session
            else:
                session = self.sessions[session_id]
                # Update client address in case it changed
                session.client_address = addr
            
            # Handle message in session
            await session.handle_message(packet, addr)
            
        except Exception as e:
            print(f"Error handling packet: {e}")

def main():
    if len(sys.argv) != 2:
        print("Usage: ./server <portnum>", file=sys.stderr)
        sys.exit(1)
    
    try:
        port = int(sys.argv[1])
    except ValueError:
        print("Port must be a number", file=sys.stderr)
        sys.exit(1)
    
    server = UAP_Server(port)
    
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nServer interrupted")
    except Exception as e:
        print(f"Server failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()