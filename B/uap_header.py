# uap_header.py
import struct
import time
import random

# --- Protocol Constants ---
UAP_MAGIC = 0xC461
UAP_VERSION = 1

# Command Codes
CMD_HELLO = 0
CMD_DATA = 1
CMD_ALIVE = 2
CMD_GOODBYE = 3

# --- Format ---
# Header: magic, version, command, seq_num, session_id, logical_clock, timestamp
#          (>H,   B,       B,       I,        I,          Q,             Q)
#          (2,    1,       1,       4,        4,          8,             8) = 28 bytes
HEADER_FORMAT = ">HBBIIQQ"
HEADER_LENGTH = struct.calcsize(HEADER_FORMAT)

# --- Timeouts ---
RTO_S = 5.0  # Retransmission Timeout in seconds for the client
INACTIVITY_TIMEOUT_S = 10.0  # Server session inactivity timeout

def curr_timestamp_us():
    """Returns the current time as a Unix timestamp in microseconds."""
    return int(time.time() * 1_000_000)

def unpack_header(packet: bytes) -> dict:
    """Unpacks the UAP header from a packet. Raises ValueError if packet is too short."""
    if len(packet) < HEADER_LENGTH:
        raise ValueError("Packet is too short to contain a valid header.")
    
    magic, version, command, seq, session_id, clock, ts = struct.unpack(HEADER_FORMAT, packet[:HEADER_LENGTH])
    return {
        "magic": magic, "version": version, "command": command,
        "sequence_number": seq, "session_id": session_id,
        "logical_clock": clock, "timestamp": ts
    }

def pack_header(command: int, sequence_number: int, session_id: int, logical_clock: int) -> bytes:
    """Packs a UAP header with the current timestamp."""
    timestamp = curr_timestamp_us()
    return struct.pack(HEADER_FORMAT, UAP_MAGIC, UAP_VERSION, command, 
                       sequence_number, session_id, logical_clock, timestamp)

# --- Message Creation Helper Functions ---

def create_hello_message(sequence_number: int, session_id: int, logical_clock: int) -> bytes:
    """Creates a complete UAP HELLO message."""
    return pack_header(CMD_HELLO, sequence_number, session_id, logical_clock)

def create_data_message(sequence_number: int, session_id: int, logical_clock: int, data: str) -> bytes:
    """Creates a complete UAP DATA message."""
    header = pack_header(CMD_DATA, sequence_number, session_id, logical_clock)
    return header + data.encode('utf-8')

def create_alive_message(sequence_number: int, session_id: int, logical_clock: int) -> bytes:
    """Creates a complete UAP ALIVE message."""
    return pack_header(CMD_ALIVE, sequence_number, session_id, logical_clock)
    
def create_goodbye_message(sequence_number: int, session_id: int, logical_clock: int) -> bytes:
    """Creates a complete UAP GOODBYE message."""
    return pack_header(CMD_GOODBYE, sequence_number, session_id, logical_clock)

def extract_data(packet: bytes) -> str:
    """Extracts the data payload from a UAP packet."""
    if len(packet) <= HEADER_LENGTH:
        return ""
    return packet[HEADER_LENGTH:].decode('utf-8', errors='ignore')

def generate_session_id() -> int:
    """Generates a random 32-bit session ID."""
    return random.randint(0, 0xFFFFFFFF)

def update_logical_clock(current_clock: int, received_clock: int) -> int:
    """Implements the Lamport logical clock update rule."""
    return max(current_clock, received_clock) + 1