import struct
import time
import random

Constants = {
    "magic": 0xC461,
    "version": 1,
    "command_HELLO": 0,
    "command_DATA": 1,
    "command_ALIVE": 2,
    "command_GOODBYE": 3
}

# Big endian 
Header_Format = ">HBBIIQQ"
Header_length = 28

# timeouts / time constants 
RTO_MS = 5000           # client wait for response
INACTIVITY_MS = 10000   # server session inactivity timeout
MAX_RETRIES = 3 

def unpack_header(packet):
    if len(packet) < Header_length:
        raise ValueError("packet is too short , doesn't even have header length")
    magic, version, command, sequence_number, session_id, logical_clock, timestamp = struct.unpack(Header_Format, packet[:Header_length])
    return {
        "magic": magic,
        "version": version,
        "command": command,
        "sequence_number": sequence_number,
        "session_id": session_id,
        "logical_clock": logical_clock,
        "timestamp": timestamp
    }

def curr_time_stamp():
    return int(time.time() * 1000000)

def pack_header(command, sequence_number, session_id, logical_clock, time_stamp=None):
    if time_stamp is None:
        time_stamp = curr_time_stamp()
    return struct.pack(Header_Format, Constants["magic"], Constants["version"], command, sequence_number, session_id, logical_clock, int(time_stamp))

def create_hello_message(sequence_number, session_id, logical_clock):
    return pack_header(Constants["command_HELLO"], sequence_number, session_id, logical_clock)

def create_data_message(sequence_number, session_id, logical_clock, data):
    header = pack_header(Constants["command_DATA"], sequence_number, session_id, logical_clock)
    return header + data.encode('utf-8')

def create_alive_message(sequence_number, session_id, logical_clock):
    return pack_header(Constants["command_ALIVE"], sequence_number, session_id, logical_clock)

def create_goodbye_message(sequence_number, session_id, logical_clock):
    return pack_header(Constants["command_GOODBYE"], sequence_number, session_id, logical_clock)

def extract_data(packet):
    if len(packet) <= Header_length:
        return ""
    return packet[Header_length:].decode('utf-8', errors='ignore')

def generate_session_id():
    return random.randint(0, 0xFFFFFFFF)

class GetMessage:
    def __init__(self, sequence_number, session_id, logical_clock, timestamp):
        self.session_id = session_id
        self.sequence_number = sequence_number
        self.logical_clock = logical_clock 
        self.timestamp = timestamp 

    @staticmethod
    def check_packet(magic, version):
        if magic != Constants["magic"] or version != Constants["version"]:
            raise ValueError("magic or version does not match")

def parse_message(packet):
    header = unpack_header(packet)
    GetMessage.check_packet(header.get("magic"), header.get("version"))
    return GetMessage(header['sequence_number'], header['session_id'], header['logical_clock'], header['timestamp'])

def update_logical_clock(current_clock, received_clock):
    return max(current_clock, received_clock) + 1