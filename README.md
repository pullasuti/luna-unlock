# luna-unlock

Bootloader `factory_mode` unlock/relock tool for the Lenovo Smart Clock
Essential (CD-4N341Y, Amlogic A113X). Pure [pyamlboot](https://github.com/superna9999/pyamlboot)
over the SoC's USB burn mode - no vendor tools, no additional hardware
beyond a USB-A to USB-A cable.

## What it does

Parks the SoC's burn-mode USB server (the brief window at power-on), then
sets the U-Boot environment variable `factory_mode` (0 = unlock, 1 = lock)
and saves it. The boot chain uses it to decide whether adb and debug
properties stay enabled:

| mode     | adb (USB & network) | boot cmdline                       |
|----------|---------------------|------------------------------------|
| unlocked | available           | `androidboot.debuggable=1 secure=0` |
| locked   | disabled            | `androidboot.debuggable=0 secure=1` |

Every step is verified: the command channel is warmed up first, each
`setenv`/`saveenv` must be ACKed, and the value is read back from memory
before the device is allowed to reboot. If any gate fails, the script holds
the burn window and exits non-zero instead of rebooting into an unverified
state.

## Requirements

- A USB-A to USB-A cable (male on both ends): the clock's socket is USB-A
- Python 3
- `pip install pyusb git+https://github.com/superna9999/pyamlboot.git`
  (the pyamlboot release on PyPI is an old 1.0.0 that lacks the needed
  API — the script checks for this)
- USB access to the device in burn mode (id `1b8e:c003`); on Linux this
  needs a udev rule or root

## Usage

```
python3 luna-unlock.py --status            # read current mode, no writes
python3 luna-unlock.py --unlock            # factory_mode 0
python3 luna-unlock.py --lock              # factory_mode 1 (asks for confirmation)
python3 luna-unlock.py --lock --yes        # ... without the prompt
```

Start the script, then power-cycle the clock (unplug power, plug back in).
It catches the burn window by itself and reports each phase.

## Notes

- Locking disables adb entirely. Make sure you have another way in (e.g.
  ssh) before you rely on a locked device.
- Unlock state survives vendor resets - to relock, run this tool with
  `--lock`.
- Unlocking/relocking never flashes anything; the only persistent change
  is the U-Boot environment variable.

## Disclaimer

For your own device only. Unlocks vendor restrictions and voids any
remaining warranty. Provided as-is.
