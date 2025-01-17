import os
import json
import time
import board
import RPi.GPIO as GPIO
import neopixel
import subprocess
import keyboard
import queue
import busio
import sys
import atexit
import signal
import random
from threading import Thread
from pexpect import spawn
from dotenv import load_dotenv
from pathlib import Path
from os import listdir
from adafruit_ht16k33 import segments

#################################### init, config, and constants
# load .env into os env
dotenv_path = Path('/home/calvin/App/.env')
load_dotenv(dotenv_path=dotenv_path)

# get .env constants
APP_PATH = os.getenv('APP_PATH')
BANKS_PATH = os.getenv('BANKS_PATH')
BANK_COUNT = int(os.getenv('BANK_COUNT'))

# load persistent state from file
f = open('state.json', 'a')
f.close()
persistent_state_data = {}
with open('./state.json', 'r') as stateFile:
  try:
    persistent_state_data = json.loads(stateFile.read())
    stateFile.close()
    # in case bank count changed and is less than the persisted index
    persistent_state_data['bank'] = min(BANK_COUNT, persistent_state_data['bank'])
  except TypeError:
    print('no')
    persistent_state_data['should_hold_clip'] = False
    persistent_state_data['should_shuffle'] = False
    persistent_state_data['bank'] = 1

def save_settings():
  with open('state.json', 'w') as stateFile:
    json.dump(persistent_state_data, stateFile)
    stateFile.close()

# pixels in neopixel array
total_pixels = 12

# software-debouncing in ms
button_bounce_time = 80

# whether or not shift is being held
shift_pressed = False

# current bank is what is playing
# pending bank is what the user is selecting via SHIFT
# on release of SHIFT current_bank should become pending_bank value
current_bank = persistent_state_data['bank'] 
pending_bank = 1

# index of the current clip within its bank
current_clip_index = -1

# how we will address pins
GPIO.setmode(GPIO.BCM)

# queue to issue commands to the mplayer slave in a thread
# add commands (including newline) and they will be executed in main clip loop
mplayer_command_queue = queue.Queue()

# create bank directories if they don't exist
for x in range(1, BANK_COUNT + 1):
    path = f'{BANKS_PATH}/{str(x).zfill(2)}'
    try:
        original_umask = os.umask(0)
        os.makedirs(path, mode=0o777, exist_ok=True)
    finally:
        os.umask(original_umask)


#################################### init 7-Segment display
# create the i2C interface.
i2c = busio.I2C(board.SCL, board.SDA)

# create the segment class
segment_display = segments.Seg7x4(i2c)

# clear the segment display
segment_display.fill(0)

# todo: remember banks
# display bank number
segment_display[0] = '0'
segment_display[1] = '1'

##################################### init neo pixels
pixels = neopixel.NeoPixel(board.D18, 12, auto_write=False, pixel_order=neopixel.GRBW)

##################################### setup power on LED
power_led_pin = 17
GPIO.setup(power_led_pin, GPIO.OUT, initial=GPIO.LOW)

##################################### mplayer start playlist
def set_files(bank):
    global files
    padded_bank_string = str(bank).zfill(2)
    files = sorted(os.listdir(BANKS_PATH + '/' + padded_bank_string + '/'))

def play_bank(bank):
    set_files(bank)
    padded_bank_string = str(bank).zfill(2)
    for index, file in enumerate(files):
      full_path = BANKS_PATH + '/' + padded_bank_string + '/' + file
      base_command = 'loadfile ' + '"'+ full_path + '"'
      if not index == 0:
          base_command += " 1"
      mplayer_command_queue.put(base_command + "\n")

# we have to init mplayer with something to play, so create a playlist out of the current bank right away
set_files(current_bank)

# get videos in bank directory as a single string
videos_string = ' '.join(sorted(map(lambda f: BANKS_PATH + '/' + str(current_bank).zfill(2) + '/' + '"' + f + '"', files)))

# start mplayer process
mplayer_spawn = spawn('/usr/bin/mplayer -fs -vo fbdev2 -osdlevel 0 -nosound -vf scale=720:480 -loop 0 -slave -quiet ' + videos_string)

##################################### button setup
mode_button_pin = 22;
prev_button_pin = 5;
play_pause_button_pin = 6;
next_button_pin = 13;
hold_button_pin = 19;
shift_button_pin = 26;
GPIO.setup(mode_button_pin,       GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(prev_button_pin,       GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(play_pause_button_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(next_button_pin,       GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(hold_button_pin,       GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(shift_button_pin,      GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

###################################### button callbacks
def change_mode():
    global persistent_state_data
    persistent_state_data['should_shuffle'] = not persistent_state_data['should_shuffle']
    save_settings()

def previous_clip():
    mplayer_command_queue.put("pt_step -1\n")

def play_pause():
    mplayer_command_queue.put("pause\n")

def next_clip():
    global persistent_state_data
    global current_clip_index
    if persistent_state_data['should_shuffle'] and current_clip_index > -1:
      mplayer_command_queue.put(f'pt_step {get_next_random_index(current_clip_index)}\n')
    elif not persistent_state_data['should_shuffle']:
      mplayer_command_queue.put("pt_step 1\n")

def hold_current_clip():
    global persistent_state_data
    persistent_state_data['should_hold_clip'] = not persistent_state_data['should_hold_clip']
    save_settings()

def pending_bank_up():
    global pending_bank
    if pending_bank == BANK_COUNT:
        pending_bank = 1
    else:
        pending_bank += 1

def pending_bank_down():
    global pending_bank
    if pending_bank == 1:
        pending_bank = BANK_COUNT
    else:
        pending_bank -= 1

def take_shift_action(button):
    if button == prev_button_pin:
       pending_bank_down()
    elif button == next_button_pin:
       pending_bank_up()

def take_standard_action(button):
    if button == mode_button_pin:
        change_mode()
    elif button == prev_button_pin:
        previous_clip()
    elif button == play_pause_button_pin:
        play_pause()
    elif button == next_button_pin:
        next_clip()
    elif button == hold_button_pin:
        hold_current_clip()

def button_pushed(button):
    global shift_pressed
    if not shift_pressed:
        take_standard_action(button)
    else:
        take_shift_action(button)

def shift_released():
    global current_bank
    global pending_bank
    global persistent_state_data
    if pending_bank != current_bank:
        current_bank = pending_bank
        play_bank(current_bank)
        persistent_state_data['bank'] = current_bank
        save_settings()

def shift_button_changed(pin):
    global shift_pressed
    shift_pressed = GPIO.input(pin)
    if not shift_pressed:
        shift_released()

GPIO.add_event_detect(mode_button_pin,       GPIO.RISING, callback=button_pushed,        bouncetime=button_bounce_time)
GPIO.add_event_detect(prev_button_pin,       GPIO.RISING, callback=button_pushed,        bouncetime=button_bounce_time)
GPIO.add_event_detect(play_pause_button_pin, GPIO.RISING, callback=button_pushed,        bouncetime=button_bounce_time)
GPIO.add_event_detect(next_button_pin,       GPIO.RISING, callback=button_pushed,        bouncetime=button_bounce_time)
GPIO.add_event_detect(hold_button_pin,       GPIO.RISING, callback=button_pushed,        bouncetime=button_bounce_time)
GPIO.add_event_detect(shift_button_pin,      GPIO.BOTH,   callback=shift_button_changed)

######################################### show power on
GPIO.output(power_led_pin, GPIO.HIGH)

######################################### neopixel updates
def neopixel_update(percent):
  global persistent_state_data

  # can be 0-255
  max_brightness = 50;

  # pixel_step is what each whole pixel represents in %
  pixel_step = 100/total_pixels

  # numPixels is the number of pixels starting from 0 to light up fully
  num_pixels = percent//pixel_step

  # remainder is how much to dimly light the next pixel
  remainder = percent % pixel_step

  brightness_step = max_brightness/pixel_step

  for pixel_index in range(total_pixels):
    if pixel_index == num_pixels:
      b = remainder * brightness_step
      if persistent_state_data['should_hold_clip']:
        pixels[pixel_index] = (0, 0, remainder * brightness_step, 0)
      elif persistent_state_data['should_shuffle']:
        pixels[pixel_index] = (remainder * brightness_step, 0, 0, 0)
      else:
        pixels[pixel_index] = (0, 0, 0, remainder * brightness_step)
    elif pixel_index > num_pixels:
      pixels[pixel_index] = (0, 0, 0, 0)
    else:
      if persistent_state_data['should_hold_clip']:
        pixels[pixel_index] = (0, 0, max_brightness, 0)
      elif persistent_state_data['should_shuffle']:
        pixels[pixel_index] = (max_brightness, 0, 0, 0)
      else:
        pixels[pixel_index] = (0, 0, 0, max_brightness)

  pixels.show()

######################################### 7-segment display updates
def clip_segment_update(clip_index):
  # padded and adjusted string of clip_index, e.g clip_index 0 becomes "01"
  padded_clip_str = str(clip_index + 1).zfill(2)
  # set display values
  segment_display[2] = padded_clip_str[0]
  segment_display[3] = padded_clip_str[1]

def bank_segment_update():
  display_bank = current_bank
  if shift_pressed:
      display_bank = pending_bank

  # padded and adjusted string of display_bank, e.g display_bank 1 becomes "01"
  padded_bank_str = str(display_bank).zfill(2)

  # set display values
  segment_display[0] = padded_bank_str[0]
  segment_display[1] = padded_bank_str[1]

def get_next_random_index(current_index):
  files_count = len(files)
  moveDistance = random.randrange(1, files_count-1)
  moveDirection = 1 
  if random.randrange(0, 2) > 0:
      moveDirection = -1
  return moveDirection * moveDistance


######################################## MAIN MPLAYER EXECUTION THREAD LOOP
def mplayer_command_thread_execute():
  global persistent_state_data
  global current_clip_index

  while True:
      # dump the command queue into the stdin
      while not mplayer_command_queue.empty():
          command = mplayer_command_queue.get()
          mplayer_spawn.write(command)
          time.sleep(0.1)
          
      # request current file playing
      mplayer_spawn.write("pausing_keep_force get_property filename\n")
      mplayer_spawn.expect_exact(["ANS_filename="])
      current_filename = str(mplayer_spawn.readline().rstrip()).replace('\'', '')[1:]

      # get the index of the filename in the global list of files
      # and use it to update the segmented display
      if current_filename in files:
          current_clip_index = files.index(current_filename)
          clip_segment_update(current_clip_index)

      bank_segment_update()

      # ensure colon is showing after updates, this can get overwritten for whatever reason
      segment_display.colon = True

      # request current percent_pos from mplayer
      mplayer_spawn.write("pausing_keep_force get_percent_pos\n")

      # wait for proper response
      mplayer_spawn.expect_exact(["ANS_PERCENT_POSITION="])

      # parse response to int 1-100
      percent = int(mplayer_spawn.readline().rstrip())

      if percent >= 99:
        if persistent_state_data['should_hold_clip']:
          # seek <value> [type]
          #  - we are using type `2` which is flagging
          #    the value `0` as an absolute position
          mplayer_spawn.write("seek 0 2\n")
        elif persistent_state_data['should_shuffle'] and current_clip_index > -1:
            mplayer_spawn.write(f'pt_step {get_next_random_index(current_clip_index)}')

      # update neopixel progress indication
      neopixel_update(percent)

      time.sleep(0.1)

mplayer_command_thread = Thread(target = mplayer_command_thread_execute)
mplayer_command_thread.daemon=True
mplayer_command_thread.start()

def clean_up():
    GPIO.output(power_led_pin, GPIO.LOW)
    segment_display[0] = "-"
    segment_display[1] = "-"
    segment_display[2] = "-"
    segment_display[3] = "-"
    neopixel_update(0)

def exit_handler():
    clean_up()

def kill_handler(*args):
    sys.exit(0)

atexit.register(exit_handler)
signal.signal(signal.SIGINT, kill_handler)
signal.signal(signal.SIGTERM, kill_handler)

keyboard.wait()
