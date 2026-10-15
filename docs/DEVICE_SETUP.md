# Device setup

The farmer container needs an Android device running Dokkan Battle that it can reach with **ADB**.
Pick one of these:

| Option | Effort | Reliability | Notes |
|---|---|---|---|
| **A. Spare phone or tablet on Wi-Fi** | low | ★★★ | Recommended. A real device, so the game is officially supported. |
| **B. Emulator on a PC** (BlueStacks, LDPlayer, MuMu) | medium | ★★☆ | The PC has to stay on. |
| **C. redroid container on the server** | high | ★☆☆ | Fully self-hosted but **experimental**: not confirmed to run Dokkan. |
| **D. Phone on USB at the server** | medium | ★★★ | Needs USB passthrough into the container. |

## Tips for every option

- Install Dokkan, link your account (so you never lose it), and **download all game data** in the
  game before farming.
- Keep the device **charging** and turn off battery optimisation for Dokkan.
- Use **no screen lock** (or swipe only). `KEEP_SCREEN_ON=true` (default) enables Android's
  "stay awake" while the farm runs, and the farmer sends WAKEUP if the screen goes dark.
- Give the device a **fixed IP** (DHCP reservation in your router) so `ADB_SERIAL` stays valid.
- Mute it: Dokkan plays sound.

## A. Phone or tablet over Wi-Fi (recommended)

1. Enable *Developer options*: Settings → About phone → tap **Build number** 7 times.
2. **Android 11 or newer: Wireless debugging**
   1. Developer options → **Wireless debugging** → on.
   2. Tap **Pair device with pairing code**. Open the farmer's **Device** tab, enter the
      *IP address & port* and the *6-digit code* from that popup, and press **Pair**.
   3. Back on the Wireless debugging screen, read **IP address & Port** (a *different* port) and
      enter it under **Connect**, e.g. `192.168.1.50:41234`.
   4. That port changes every time wireless debugging is toggled or the phone restarts; enter the
      new one in the Device tab when that happens. For a **fixed port**, connect the phone to any
      computer with USB once and run `adb tcpip 5555`. Then use `192.168.1.50:5555` until the next
      reboot.
3. **Android 10 or older**: connect it with USB to any computer once, run `adb tcpip 5555`, unplug,
   and use `ADB_SERIAL=<phone-ip>:5555`.
4. On the first connection the phone asks **"Allow debugging?"**: tick *Always allow* and accept.
   The farmer's adb key is stored in the data volume, so this survives updates.

The container uses Docker's default network, which can reach devices on your LAN. If it can't,
check for "client isolation" / "AP isolation" on your Wi-Fi.

## B. Emulator on a PC

Enable ADB in the emulator:

- **BlueStacks 5**: Settings → Advanced → *Android Debug Bridge (ADB)* → on (shows the port).
- **LDPlayer**: Settings → Other settings → *ADB debugging* → *Open local connection*.
- **MuMu Player**: the ADB port is shown in the multi-instance manager / settings.

Use a **portrait** resolution (e.g. 720x1280 or 1080x1920), because Dokkan is a portrait game.

Emulators only listen on `127.0.0.1`, so make the port reachable:

- **Docker on the same PC** (Docker Desktop): `ADB_SERIAL=host.docker.internal:5555`.
- **Farmer on another machine**: forward the port on Windows (PowerShell as admin), then allow it in
  the firewall:

  ```powershell
  netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=5555 connectaddress=127.0.0.1 connectport=5555
  ```

  and use `ADB_SERIAL=<pc-ip>:5555`.

## C. redroid: Android in a container (experimental)

[redroid](https://github.com/remote-android/redroid-doc) runs Android in Docker next to the farmer
([`docker-compose.redroid.yml`](../docker-compose.redroid.yml)). It is the only fully self-hosted
option, but **nobody has confirmed that Dokkan works on it**. The game may refuse a rooted or
emulated system, and software rendering can be slow. Try it if you like tinkering.

Requirements:

- A **Linux host whose kernel has binder**. On Ubuntu/Debian:

  ```sh
  sudo apt install linux-modules-extra-$(uname -r)
  sudo modprobe binder_linux devices="binder,hwbinder,vndbinder"
  echo binder_linux | sudo tee /etc/modules-load.d/redroid.conf
  echo 'options binder_linux devices="binder,hwbinder,vndbinder"' | sudo tee /etc/modprobe.d/redroid.conf
  ```

  This does **not** work on Docker Desktop (Windows/macOS) or on most NAS kernels. Kernels that only
  provide *binderfs* are a [known problem](https://github.com/remote-android/redroid-doc/issues/859).
- **ARM support.** Dokkan ships ARM code. On an **ARM64 host** (Raspberry Pi 5, Ampere, ...) it runs
  natively. On **x86_64** you need an image with ARM translation (libndk). Build one with
  [redroid-script](https://github.com/ayasa520/redroid-script), e.g.
  `python redroid.py -a 12.0.0_64only -n` (add `-mtg` for Google Play). Set
  `REDROID_IMAGE` to the image it creates and uncomment the `ro.*` lines in
  `docker-compose.redroid.yml`.

Installing Dokkan on redroid:

- With a GApps image, sign in to the Play Store using the farmer's **Screen** tab (Touch control).
- Or copy the APKs from your phone. From a PC with adb:

  ```sh
  adb -s <phone> shell pm path com.bandainamcogames.dbzdokkanww   # lists base.apk + split APKs
  adb -s <phone> pull <each path>
  adb -s <redroid-host>:5555 install-multiple base.apk split_*.apk
  ```

  (Expose port 5555 of the redroid service for this, see the comment in the compose file.)

Then log in, download the data and set up your stage with the Screen tab, exactly like on a phone.

## D. Phone on USB at the server

1. Enable *USB debugging* in Developer options and plug the phone into the server.
2. In the compose file uncomment `devices: - /dev/bus/usb:/dev/bus/usb` and set
   `RUN_AS_ROOT: "true"` (USB device nodes are root-owned).
3. Leave `ADB_SERIAL` empty (it picks the only device) or set the serial from the Device tab's
   *ADB devices* list.
4. Make sure no other adb server on the host grabs the phone (`adb kill-server` on the host).
