#!/usr/bin/env python3

import socket
import sys
import time
from uap_header import *

class BasicServer:
    def __init__(self, port):
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('', port))
        self.sessions = {}  # session_id -> session_info
        self.server_sequence = 0
        self.logical_clock = 0
        
    def run(self):
        print(f"Waiting on port {self.port}...")
        
        while True:
            try:
                # Receive packet
                packet, addr = self.socket.recvfrom(4096)
                self.handle_packet(packet, addr)
                
            except KeyboardInterrupt:
                print("\nServer shutting down...")
                self.cleanup()
                break
            except Exception as e:
                print(f"Error handling packet: {e}")
    
    def handle_packet(self, packet, addr):
        try:
            # Parse the packet
            header = unpack_header(packet)
            message = parse_message(packet)
            
            # Update logical clock
            self.logical_clock = update_logical_clock(self.logical_clock, message.logical_clock)
            
            session_id = message.session_id
            command = header['command']
            seq_num = header['sequence_number']
            
            # Handle session
            if session_id not in self.sessions:
                if command == Constants["command_HELLO"]:
                    self.create_session(session_id, addr, seq_num)
                else:
                    # Invalid first message
                    return
            elif command != Constants["command_HELLO"]:  # Only handle non-HELLO messages for existing sessions
                session = self.sessions[session_id]
                self.handle_message_in_session(session, command, seq_num, packet)
            
        except Exception as e:
            print(f"Error parsing packet: {e}")
    
    def create_session(self, session_id, addr, seq_num):
        """Create a new session"""
        session = {
            'id': session_id,
            'addr': addr,
            'state': 'START',
            'expected_seq': 0,
            'last_activity': time.time()
        }
        self.sessions[session_id] = session
        print(f"0x{session_id:08x} [{seq_num}] Session created")
        
        # Send HELLO response
        self.send_hello_response(session)
        session['state'] = 'RECEIVE'
    
    def handle_message_in_session(self, session, command, seq_num, packet):
        """Handle message within a session context"""
        session_id = session['id']
        session['last_activity'] = time.time()
        
        if command == Constants["command_HELLO"]:
            if session['state'] == 'RECEIVE':
                # Unexpected HELLO - protocol error
                print(f"0x{session_id:08x} Protocol error: unexpected HELLO")
                self.close_session(session_id)
                return
        
        elif command == Constants["command_DATA"]:
            if session['state'] == 'RECEIVE':
                # Check sequence number
                if seq_num > session['expected_seq']:
                    # Lost packets
                    for lost_seq in range(session['expected_seq'], seq_num):
                        print(f"0x{session_id:08x} [{lost_seq}] Lost packet!")
                elif seq_num < session['expected_seq']:
                    # Old packet - protocol error
                    print(f"0x{session_id:08x} Protocol error: old sequence number")
                    self.close_session(session_id)
                    return
                elif seq_num == session['expected_seq'] - 1:
                    # Duplicate packet
                    print(f"0x{session_id:08x} [{seq_num}] Duplicate packet!")
                    return
                
                # Extract and print data
                data = extract_data(packet)
                print(f"0x{session_id:08x} [{seq_num}] {data}")
                
                # Send ALIVE response
                self.send_alive_response(session)
                session['expected_seq'] = seq_num + 1
        
        elif command == Constants["command_GOODBYE"]:
            print(f"0x{session_id:08x} [{seq_num}] GOODBYE from client.")
            self.send_goodbye_response(session)
            self.close_session(session_id)
    
    def send_hello_response(self, session):
        """Send HELLO response to client"""
        self.logical_clock += 1
        response = create_hello_message(self.server_sequence, session['id'], self.logical_clock)
        self.socket.sendto(response, session['addr'])
        self.server_sequence += 1
    
    def send_alive_response(self, session):
        """Send ALIVE response to client"""
        self.logical_clock += 1
        response = create_alive_message(self.server_sequence, session['id'], self.logical_clock)
        self.socket.sendto(response, session['addr'])
        self.server_sequence += 1
    
    def send_goodbye_response(self, session):
        """Send GOODBYE response to client"""
        self.logical_clock += 1
        response = create_goodbye_message(self.server_sequence, session['id'], self.logical_clock)
        self.socket.sendto(response, session['addr'])
        self.server_sequence += 1
    
    def close_session(self, session_id):
        """Close and remove a session"""
        if session_id in self.sessions:
            print(f"0x{session_id:08x} Session closed")
            del self.sessions[session_id]
    
    def cleanup(self):
        """Send GOODBYE to all active sessions and cleanup"""
        for session_id, session in list(self.sessions.items()):
            self.send_goodbye_response(session)
            self.close_session(session_id)
        self.socket.close()

def main():
    if len(sys.argv) != 2:
        print("Usage: ./server <portnum>")
        sys.exit(1)
    
    try:
        port = int(sys.argv[1])
        server = BasicServer(port)
        server.run()
    except ValueError:
        print("Error: Port must be a number")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()