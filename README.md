
Network Programming / UDP
=====================================================

Team Members:
- [PASUPULETI SANJAY(112201028)] (Partner A)
- [KANIKELLA PRAMODH(112201029)] (Partner B)

--------------------
| Project Overview |
--------------------

This project implements a custom UDP Application Protocol (UAP) for a client-server data transfer application. The protocol is designed to handle concurrency, sessions, reliable acknowledgments (DATA/ALIVE), and graceful termination (GOODBYE), all over the connectionless UDP transport layer.

The core goal of this assignment is to gain hands-on experience with two distinct approaches to concurrency in network programming:

1.  **Thread-based Concurrency:** Using threads as the primary mechanism for handling concurrent tasks, where each thread blocks on a single event source (e.g., network input or keyboard input).

2.  **Event-driven Concurrency (Non-blocking I/O):** Using a single thread to listen to multiple event sources simultaneously, reacting to events as they occur without blocking.

As per the assignment specification, the work was divided between two partners to ensure exposure to both models and to test interoperability between different implementations.

--------------------------------
| Division of Work & Structure |
--------------------------------

The project is structured according to the roles assigned in the lab document:

*   **Partner A ([Pasupuleti Sanjay]):**
    *   Implemented the **Thread-based Client**.
    *   Implemented the **Not thread-based (Event-driven) Server**.

*   **Partner B ([Pramodh Sai]):**
    *   Implemented the **Not thread-based (Event-driven) Client**.
    *   Implemented the **Thread-based Server**.

The final submission is organized into the required directory structure:

Sanjay-PramodhLab-3
A(Client Threaded and Non Threaded Server)


B(Client Non-Threaded and server Threaded)


-----------------------------
| Implementation Details    |
-----------------------------

Partner A :
----------------------------
*   **Not thread-based Server (`A/server.py`):**
    - Implemented using Python's `asyncio` library to create a single-threaded, event-driven server.
    - The `asyncio.DatagramProtocol` class is used to handle network events.
    - Each client session is managed as a distinct object with its own state and Finite State Automata (FSA), but all I/O and logic are processed concurrently within the single event loop, eliminating thread management overhead and race conditions.

*   **Thread-based Client (`A/client.py`):**
    - The main thread manages the client's FSA.
    - Two background daemon threads are used to handle blocking I/O:
        1. A `_network_reader` thread blocks on `socket.recvfrom()`.
        2. A `_stdin_reader` thread blocks on reading lines from `sys.stdin`.
    - `queue.Queue` is used for safe communication between threads.
    - A `threading.Event` synchronizes the initial handshake, and a bounded queue (`maxsize=100`) applies back-pressure to prevent overwhelming the network during high-volume file transfers.

Partner B :
---------------------------------
*   **Thread-based Server (`B/server.py`):**
    - This server uses a main thread to listen for all incoming packets.
    - Upon receiving a `HELLO` from a new client, it spawns a dedicated `SessionThread` to manage that client's entire session and FSA, isolating it from other clients.

*   **Not thread-based Client (`B/client.py`):**
    - This client uses a single-threaded, event-driven model (e.g., using `asyncio` or `select`).
    - An event loop concurrently waits for I/O readiness on both the network socket (for server responses) and stdin (for user input), managing the client FSA without multi-threading.

Shared Components:
------------------
*   **`uap_header.py`:** This shared module contains all UAP protocol definitions, including the magic number (0xC461), version, command codes, header `struct` format, and helper functions for packing/unpacking messages. This ensures perfect interoperability between all client and server implementations.


--------------------------
| How to Run the Code    |
--------------------------

**Prerequisites:** Python 3.7+ is recommended. No external libraries are needed.

**Step 1: Grant Execute Permissions**
Before running, navigate to the project root and make all scripts executable:
$ chmod +x A/*.py B/*.py

**Step 2: Running a Server**
Navigate into the desired directory (`A/` or `B/`) and execute the server script with a port number.

Example (running Partner A's Not thread-based Server):
$ cd A/
$ ./server.py 1234

**Step 3: Running a Client**
In a separate terminal, navigate into the desired directory and execute the client script with the server's hostname and port.

Example (running Partner A's Thread-based Client):
$ cd A/
$ ./client.py localhost 1234


----------------------------------
| Required Tests and Execution   |
----------------------------------

All tests must be performed for all four combinations of client and server to verify full interoperability.

**Test 1: Basic Typed Input**
This test verifies basic data transfer and correct server output formatting.

1.  Start any server: `./server.py 1234`
2.  Start any client: `./client.py localhost 1234`
3.  In the client, type "one", "two", "three", pressing Enter after each.
4.  Press `Ctrl+D` to signal end-of-file. The client should print "eof" and exit.

**Expected Server Output (session ID will vary):**


**Test 2: Input Redirected from File (Packet Loss Test)**
This tests high-volume data transfer and the server's ability to detect lost packets.

1.  Ensure the `Dostoyevsky.txt` file is available.
2.  Start any server: `./server.py 1234`
3.  Run any client with redirected input:
    $ ./client.py localhost 1234 < Dostoyevsky.txt

**Expected Server Output:**
The server output should show lines from the book. Due to UDP packet loss under load, it will correctly identify and report gaps in the client sequence numbers.



**Test 3: Concurrent Clients**
This test verifies the server's ability to handle multiple clients at once.

1.  Create a shell script `run_concurrent.sh` in the client's directory:
    ```bash
    #!/bin/bash
    ./client.py localhost 1234 < Dostoyevsky.txt > dual-c1.out 2>&1 &
    ./client.py localhost 1234 < Dostoyevsky.txt > dual-c2.out 2>&1 &
    ```
2.  Make it executable: `chmod +x run_concurrent.sh`
3.  Start any server, redirecting its output to a file: `./server.py 1234 | tee server_out.txt`
"OPTIONAL"
4.  Run the script: `./run_concurrent.sh`
5.  After it finishes, inspect the log: `cat server.log`

**Expected Server Output:**
The log must show interleaved output from two different session IDs, confirming that the server was handling both clients concurrently.
