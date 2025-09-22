#!/usr/bin/env -S python3 -u
import asyncio
import sys
from enum import Enum, auto
from typing import Dict, Tuple
from uap_header import *

class SessionState(Enum):
    """Enumeration for the server-side session state machine."""
    WAIT_HELLO = auto()
    ESTABLISHED = auto()
    DONE = auto()

class Session:
    """
    Manages the state and logic for a single client session.
    Each Session object is a state machine, but all are managed concurrently
    by the single event loop in UAPServerProtocol.
    """
    def __init__(self, session_id: int, client_address: Tuple[str, int], 
                 transport: asyncio.DatagramTransport, on_close: callable):
        self.session_id = session_id
        self.client_address = client_address
        self.transport = transport
        self.on_close = on_close # Callback to remove session from the server's main dictionary
        
        self.state = SessionState.WAIT_HELLO
        self.expected_client_seq = 0
        self.server_seq = 0
        self.logical_clock = 0
        self.timer_task = None
        self._reset_inactivity_timer()

    def _reset_inactivity_timer(self):
        """Resets the 10-second inactivity timer for this session."""
        if self.timer_task:
            self.timer_task.cancel()
        self.timer_task = asyncio.create_task(self._inactivity_timeout())
    
    async def _inactivity_timeout(self):
        try:
            await asyncio.sleep(INACTIVITY_TIMEOUT_S)
            print(f"0x{self.session_id:08x} Session timed out due to inactivity")
            await self.close(send_goodbye=True)
        except asyncio.CancelledError:
            pass # Timer was reset, which is normal
    
    async def close(self, send_goodbye: bool = False):
        """Closes the session, optionally sending a GOODBYE message."""
        if self.state == SessionState.DONE:
            return
        
        if send_goodbye:
            self.logical_clock += 1
            goodbye_msg = create_goodbye_message(self.server_seq, self.session_id, self.logical_clock)
            self.transport.sendto(goodbye_msg, self.client_address)
        
        self.state = SessionState.DONE
        if self.timer_task:
            self.timer_task.cancel()
        
        print(f"0x{self.session_id:08x} Session closed")
        self.on_close(self.session_id)

    async def handle_message(self, data: bytes):
        """Processes an incoming datagram according to the session's current state (FSA)."""
        try:
            header = unpack_header(data)
        except ValueError:
            return # Malformed packet, discard silently

        self.logical_clock = update_logical_clock(self.logical_clock, header["logical_clock"])
        command = header["command"]
        client_seq = header["sequence_number"]

        if self.state == SessionState.ESTABLISHED:
            self._reset_inactivity_timer()

        # --- FSA Logic ---
        if self.state == SessionState.WAIT_HELLO:
            if command == CMD_HELLO and client_seq == 0:
                print(f"0x{self.session_id:08x} [0] Session created")
                self.logical_clock += 1
                hello_resp = create_hello_message(self.server_seq, self.session_id, self.logical_clock)
                self.transport.sendto(hello_resp, self.client_address)
                self.server_seq += 1
                self.expected_client_seq = 1
                self.state = SessionState.ESTABLISHED
            else:
                await self.close() # Protocol error

        elif self.state == SessionState.ESTABLISHED:
            if command == CMD_GOODBYE:
                print(f"0x{self.session_id:08x} [{client_seq}] GOODBYE from client.")
                await self.close(send_goodbye=False) # Client initiated, no need to send back
            elif command == CMD_DATA:
                await self._handle_data(client_seq, data)
            else:
                await self.close() # Protocol error (e.g., HELLO in established state)

    async def _handle_data(self, client_seq: int, packet: bytes):
        if client_seq < self.expected_client_seq:
            # This could be a duplicate or an old, out-of-order packet.
            # A robust implementation might just re-ACK, but for this lab, we can log it.
            print(f"0x{self.session_id:08x} [{client_seq}] Duplicate or old packet received!")
            # Send another ALIVE for the last successful sequence to help client resync
            self.logical_clock += 1
            alive_msg = create_alive_message(self.server_seq, self.session_id, self.logical_clock)
            self.transport.sendto(alive_msg, self.client_address)
            return
        
        if client_seq > self.expected_client_seq:
            # This indicates one or more packets were lost.
            for lost_seq in range(self.expected_client_seq, client_seq):
                print(f"0x{self.session_id:08x} [{lost_seq}] Lost packet!")

        # Process the current (or recovered) packet
        payload = extract_data(packet)
        print(f"0x{self.session_id:08x} [{client_seq}] {payload}")
        
        self.logical_clock += 1
        self.server_seq +=1
        alive_msg = create_alive_message(self.server_seq, self.session_id, self.logical_clock)
        self.transport.sendto(alive_msg, self.client_address)
        
        self.expected_client_seq = client_seq + 1

class UAPServerProtocol(asyncio.DatagramProtocol):
    """
    The main asyncio protocol handler. It acts as the single-threaded event listener.
    It does not manage session state itself; instead, it creates and delegates packets
    to the appropriate Session object, enabling concurrent session handling.
    """
    def __init__(self, on_shutdown: asyncio.Event):
        self.sessions: Dict[int, Session] = {}
        self.transport = None
        self.on_shutdown = on_shutdown

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        try:
            header = unpack_header(data)
            if header['magic'] != UAP_MAGIC or header['version'] != UAP_VERSION:
                return # Silently discard packets with wrong magic/version
            
            session_id = header['session_id']
            session = self.sessions.get(session_id)
            
            if not session:
                # If it's a new session, it must be a HELLO packet
                if header['command'] == CMD_HELLO:
                    session = Session(session_id, addr, self.transport, self._remove_session)
                    self.sessions[session_id] = session
                else:
                    return # Ignore non-HELLO packets for non-existent sessions

            session.client_address = addr # Update address in case of NAT/port change
            # Schedule the message handling as a new task in the event loop
            asyncio.create_task(session.handle_message(data))

        except (ValueError, KeyError):
            pass # Ignore malformed packets

    def _remove_session(self, session_id: int):
        """Callback for Session objects to remove themselves from the active list."""
        if session_id in self.sessions:
            del self.sessions[session_id]

    def connection_lost(self, exc):
        print("Server socket closed.")
        self.on_shutdown.set()

async def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: {sys.argv[0]} <portnum>")

    try:
        port = int(sys.argv[1])
    except ValueError:
        sys.exit(f"Error: Port '{sys.argv[1]}' is not a valid number.")

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    print(f"Event-driven server waiting on port {port}...")
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UAPServerProtocol(shutdown_event),
        local_addr=('0.0.0.0', port))

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        print("Shutting down server...")
        # Gracefully close all active sessions
        for session in list(protocol.sessions.values()):
            await session.close(send_goodbye=True)
        transport.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer interrupted by user.")