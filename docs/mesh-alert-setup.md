# Finishing the LoRa alert path

Everything is written and installed; it is inert until two commands are run as
root on the opi. Written down on 2026-09-06 so picking this up costs nothing.

## What is already done

- `bin/dokku-reconcile.sh` — heals what it can, names what it cannot, installed
  and running every ten minutes as a user timer.
- `bin/mesh-alert.sh` — sends one line over LoRa. Fails silently at every step
  by design: no radio, no cli, no permission on the port all mean "say nothing",
  because an alert that can crash the watchdog is worse than no alert.
- `dokku-reconcile-alert.service` — fires `OnFailure` of the reconcile, carrying
  the last line it wrote.

## The two commands

```sh
sudo apt install -y pipx
sudo usermod -aG dialout jimmy
```

Then, as jimmy:

```sh
pipx install meshtastic
pipx ensurepath
sudo loginctl terminate-user jimmy   # the group must reach the user units
```

`pip install` does not work here: this Ubuntu is externally managed (PEP 668)
and refuses. `pipx` is the tool, and it is what puts the cli on PATH.

The `dialout` group is what lets anything open `/dev/ttyUSB0`. It needs a fresh
login, and the systemd **user** session has to restart too — otherwise the alert
unit still gets permission denied and it looks broken rather than unconfigured.

## The last step

The node to tell has to be named, or nothing is sent — deliberately, so the
estate's state is never broadcast to whoever happens to be listening.

```sh
meshtastic --nodes                    # read the id, of the form !a1b2c3d4
mkdir -p ~/.config
echo 'MESH_DEST=!xxxxxxxx' > ~/.config/mesh-alert.env
```

Prove it end to end without waiting for a real fault:

```sh
systemctl --user start dokku-reconcile-alert.service
journalctl --user -u dokku-reconcile-alert.service -n 5 --no-pager
```

## The hardware, as found

`/dev/ttyUSB0`, a Silicon Labs CP210x UART Bridge (`10c4:ea60`) — the usual
Meshtastic serial chip. It was already plugged in.

## What this does not cover

It reports when the **reconcile** fails. Nothing on the opi can report that the
opi lost power, which is what actually happened. That wants a heartbeat a phone
notices the absence of, not an alert the box sends, and it is a different design.
