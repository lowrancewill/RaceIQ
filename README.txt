RACEIQ APP
==========
Run: pip install -r requirements.txt
python -m uvicorn main:app --reload
In VSCode use "Ctrl C" to close app

RACEIQ RACE LOGGER
==================

race_logger.py records your driving in a racing game and saves each completed
lap to a CSV file that the RaceIQ web app can read.

Supported games (Windows PC):
  - Assetto Corsa
  - Assetto Corsa Competizione (ACC)
  - F1 25

It works out which game you are playing on its own.


WHAT YOU NEED
-------------
  - Windows, and Python (the same one you use for RaceIQ)
  - numpy (already installed with the RaceIQ requirements)
  - race_logger.py saved in your RaceIQ project folder


ONE-TIME SETUP
--------------
Assetto Corsa and ACC: nothing to set up.

F1 25: turn on UDP telemetry (this is how the game sends data out).
  1. In F1 25 go to:  Game Options > Settings > UDP Telemetry Settings
  2. Set these:
        UDP Telemetry ......... On
        Broadcast Mode ........ Off
        IP Address ............ 127.0.0.1
        Port .................. 20777
        Send Rate ............. 60 Hz   (20 Hz works but is less detailed)
        UDP Format ............ 2026    (2025 also works)
  3. The first time you run the logger, Windows may ask whether to allow
     network access. Click Allow.


RECORDING LAPS
--------------
1. Open a PowerShell window in your RaceIQ project folder.
   (Use a separate window from the one running the web app.)

2. Start the logger (it can be started before or after the game):

       python race_logger.py

   It says "Waiting for a game..." until it finds one, then
   "Detected <game>".

3. Drive. Each time you cross the finish line, the lap you just finished
   is saved and the screen shows the lap time.

4. Press Ctrl+C to stop. The lap you are in the middle of is not saved.

5. Your file is in the "recordings" folder, named like:
       f1_monza_f1_25_20261013_205038.csv

6. Open the RaceIQ page in your browser and upload that file.


CHECKING THAT IT WORKS (do this the first time for each game)
--------------------------------------------------------------
Start a session in the game, then run:

       python race_logger.py --check

It prints live readings for 20 seconds. While driving, make sure:
  - "live" says yes
  - speed matches the game's speedometer
  - gas and brake go from 0 to 1 as you press the pedals
  - gear and rpm look right (gear: 1 = first, 0 = neutral, -1 = reverse)
  - trackPos goes from 0 up to 1 over one lap

If something looks wrong, tell me what the game is and paste the output.


OPTIONS
-------
  --game ac | acc | f1 | auto    Choose the game instead of auto-detecting
  --out FOLDER                   Save files somewhere else
  --check                        Show live readings instead of recording
  --f1-port NUMBER               Use a different F1 UDP port (default 20777)

Examples:
       python race_logger.py --game f1
       python race_logger.py --out C:\Users\you\Documents\laps


WHAT GETS SAVED
---------------
  - Only completed laps. A lap that is cut short is skipped.
  - Laps that go through the pit lane (in-laps and out-laps) and laps the game
    marks invalid (track limits, flashbacks) ARE saved, but flagged. RaceIQ
    shows them in the lap table and leaves them out of best-lap, consistency
    and shift statistics.
  - Lap numbers in RaceIQ count the laps saved in the file, starting at 0.
  - Each file is for one game, car and track. If you switch car or track, a
    new file starts automatically.


TROUBLESHOOTING
---------------
"Waiting for a game..." never changes
  - Assetto Corsa / ACC: the game needs to be in a session (on track or in
    the pits), not just the main menu.
  - F1 25: check the UDP settings above. Make sure UDP Telemetry is On, the
    port is 20777, and that Windows Firewall is not blocking Python.
  - Try forcing the game:  python race_logger.py --game f1

"Could not listen on UDP port 20777"
  - Another program (for example SimHub) is already using that port. Close it,
    or change the F1 port in the game and use --f1-port.

"UDP format ... is not supported"
  - In F1 25's UDP settings, set UDP Format to 2026 (or 2025).

"packet type ... has size ..., expected ..."
  - The game is sending a layout the logger does not recognise. Copy the
    message and send it to me so I can add support.

"Lap skipped (covered only NN% of the track)"
  - The lap was cut short, for example by a restart or by leaving the track
    session. That is normal.

No file appears
  - A file is only created once at least one full lap has been saved.

The track map or numbers look wrong
  - Run --check and compare with what the game shows (see above).


GOOD TO KNOW
------------
  - The logger only reads data. It does not change anything in the game.
  - F1 25 on a console can also work: set the console's UDP IP address to
    your PC's address and run the logger on the PC. (Not tested.)
  - ACC support has not been tested against the real game. If --check shows
    odd values in ACC, please report them.
