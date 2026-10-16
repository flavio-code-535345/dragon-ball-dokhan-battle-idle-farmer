# Device setup

The farmer container needs an Android device running Dokkan Battle that it can reach with **ADB**.
Pick one of these:

| Option | Effort | Reliability | Notes |
|---|---|---|---|
| **A. Spare phone or tablet on Wi-Fi** | low | ★★★ | Recommended. A real device, so the game is officially supported. |
| **B. Emulator on a PC** (BlueStacks, LDPlayer, MuMu) | medium | ★★☆ | The PC has to stay on. |
| **C. All-in-one: Android on the server** | low | ★★☆ | Nothing else has to stay on. Emulator (needs KVM, e.g. Proxmox) or redroid (needs binder). |
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

## C. All-in-one: Android on the server

Android runs in a container next to the farmer, so no phone or PC has to stay on. There are two
stacks; deploy one of them instead of `docker-compose.yml`:

| Stack | Android | Needs | Good for |
|---|---|---|---|
| [`docker-compose.emulator.yml`](../docker-compose.emulator.yml) | Google's emulator: Android 11 **with Google Play** | `/dev/kvm` (hardware virtualization) | **Proxmox** (Docker in an LXC container), most PCs and servers |
| [`docker-compose.redroid.yml`](../docker-compose.redroid.yml) | redroid: Android 14, no Google Play | a Linux host where Docker may load the binder driver | Docker directly on Ubuntu/Debian, lighter on RAM |

### C1. Emulator (recommended)

[HQarroum/docker-android](https://github.com/HQarroum/docker-android) runs Google's Android
emulator headless in a container. Its Android 11 Google Play image translates both 64-bit and
32-bit ARM code (`libndk_translation`), so Dokkan runs, and the Play Store and Google sign-in
work like on a phone. The [emulator end-to-end workflow](../.github/workflows/emulator-e2e.yml)
tests it on a GitHub runner: Android boots in about 40 seconds, the farmer connects, installs
Dokkan and the game starts and shows its first screen.

1. **KVM.** The container needs `/dev/kvm`. Docker in a **Proxmox LXC container** cannot see it
   by default; on the Proxmox host run (102 = the container's ID, see `pct list`):

   ```sh
   pct set 102 -dev0 /dev/kvm,mode=0666 && pct reboot 102
   ```

   Directly on Linux: `ls -l /dev/kvm` must exist (enable virtualization in the BIOS).
2. Deploy [`docker-compose.emulator.yml`](../docker-compose.emulator.yml) (Portainer: paste it
   into the *Web editor*). Set `WEB_PASSWORD`, `TZ` and, if needed, `HOST_PORT`. The first start
   downloads the emulator (about 2.5 GB); Android then boots in a minute or two.
3. **Device** tab: *connected*, *ARM apps: ✅ translated*.
4. **Screen** tab → *Touch control* on → open the Play Store, sign in with your Google account
   (type with the text field under the screen) and install Dragon Ball Z Dokkan Battle.
5. ▶ *Launch game*, sign in or transfer your account, download all game data, then continue with
   the stage and template setup like on a phone (README, steps 4 to 6).

Settings: `ANDROID_MEMORY` (MB, default 6144) and `ANDROID_CORES` (default 4). The emulator
renders on the CPU.

### C2. redroid

[redroid](https://github.com/remote-android/redroid-doc) runs Android itself as a container. It
is lighter than the emulator but needs the host kernel's binder driver, which Docker inside an
unprivileged LXC container (Proxmox) cannot use: take the emulator there. The stack has three
services:

1. **host-setup** runs once at every deploy. Android needs the *binder* driver of the server's
   kernel: it loads it (`modprobe binder_linux`) and writes `/etc/modules-load.d/redroid.conf` and
   `/etc/modprobe.d/redroid.conf` so the server loads it after a reboot, too. Delete those two
   files to undo that.
2. **redroid** is Android 14 at 720x1280. Its x86_64 image translates ARM code
   (`libndk_translation`), so Dokkan runs on Intel and AMD servers: the Device tab shows
   *ARM apps: ✅ translated*. Keep version 14: the newer x86_64 images (15, 16) come without that
   translator ([redroid-doc#933](https://github.com/remote-android/redroid-doc/issues/933)).
   On ARM64 servers the game runs natively.
3. **farmer** connects to `redroid:5555` by itself and waits up to 5 minutes for Android to boot.

#### Requirements

- A **Linux** server or VM (x86_64 or ARM64), ~4 GB of free RAM and ~15 GB of disk for Android,
  the game and its data. 4+ CPU cores are recommended.
- A kernel with **binder** (`CONFIG_ANDROID_BINDER_IPC`, module `binder_linux`; binderfs works
  too). host-setup loads it and stops the stack with instructions if it is missing:
  - Ubuntu: `sudo apt install linux-modules-extra-$(uname -r)`, then redeploy.
  - Proxmox: use the emulator (C1). Docker in an unprivileged LXC container cannot run redroid,
    even with binder loaded on the Proxmox host.
  - Docker Desktop (Windows/macOS), Synology and Unraid kernels usually lack binder: use option A
    or B there.
- The stack starts two **privileged** containers (host-setup and redroid). Only deploy it on a
  machine you control.

#### Setup

1. Portainer → *Stacks* → *Add stack* → paste
   [`docker-compose.redroid.yml`](../docker-compose.redroid.yml) into the *Web editor* (or use
   *Repository* with that compose path). Set `WEB_PASSWORD`, `TZ` and, if port 8080 is taken,
   `HOST_PORT`. Deploy.
2. The first start downloads Android (about 900 MB) and boots it in a minute or two. The
   **Device** tab then shows *connected* and *ARM apps: ✅ translated*.
3. **Install the game.** redroid has no Google Play, so: Device tab → *Apps* →
   **Install Aurora Store** (an open-source Play Store client, downloaded from F-Droid), then
   **Open Aurora Store**. The farmer switches to the Screen tab with *Touch control* on.
   In Aurora Store go through the welcome screens (allow it to install apps), log in
   **anonymously**, search for *Dokkan* and install *Dragon Ball Z Dokkan Battle*. If Aurora
   answers *"Access denied! Are you using VPN or Tor?"*, turn off a VPN on the server or log in
   with a Google account instead (type with the text field under the screen).
   Alternatively install an `.apk`, `.xapk` or `.apks` file from a link or an upload in the *Apps*
   card. Uploads through Cloudflare are limited to 100 MB: use the server's local address for
   bigger files.
4. **Screen tab → ▶ Launch game.** Bring your account over with the game's own data transfer
   (Bandai Namco ID or a transfer code). The text field under the screen types into the selected
   input (plain ASCII), ⌫ deletes and ↵ presses Enter. Signing in with Google is not possible
   because redroid has no Google Play services.
5. Download all game data in the game, then continue with the stage and template setup like on a
   phone (README, steps 4 to 6).

#### Performance

- `REDROID_GPU_MODE=guest` (default) renders on the CPU. It works everywhere and costs a few CPU
  cores while the game is on screen.
- `REDROID_GPU_MODE=host` renders on the server's GPU (Intel iGPU with the `i915` driver, AMD) and
  saves CPU. If the screen stays black, go back to `guest`.
- `REDROID_FPS` (default 30) can be lowered to save more CPU.

#### Good to know

- **What has been tested.** The
  [Android end-to-end workflow](../.github/workflows/android-e2e.yml) runs this stack on a GitHub
  runner: host-setup loads binder, Android boots in seconds, the farmer connects, installs
  Aurora Store and opens it. The game itself could not be tested there (Aurora's anonymous login
  refuses datacenter IPs), so the first real run of Dokkan on redroid is yours. Users of Waydroid
  (a similar Android container) report that Dokkan runs with this kind of ARM translation, some
  with occasional *"Download failed"* during the big data download (retry) or crashes. If Dokkan
  does not work for you, use option A or B: the farmer works the same.
- Only **64-bit ARM** code is translated. Install the game's `arm64-v8a` build: Aurora Store
  (Google Play) picks it by itself, and the *Apps* card refuses files that only contain 32-bit
  ARM code (`armeabi-v7a`, common on APK mirror sites). A 32-bit build would show *"This app
  isn't compatible with the latest version of Android"* and close.
- redroid is a development build of Android (*userdebug*, with `su` inside). Games that refuse
  rooted devices may notice that.
- ADB of redroid is an unprotected root shell. The compose file does not publish port 5555; if
  you open it for scrcpy, keep it inside your LAN.
- To start over, remove the stack together with its `redroid-data` volume.

## D. Phone on USB at the server

1. Enable *USB debugging* in Developer options and plug the phone into the server.
2. In the compose file uncomment `devices: - /dev/bus/usb:/dev/bus/usb` and set
   `RUN_AS_ROOT: "true"` (USB device nodes are root-owned).
3. Leave `ADB_SERIAL` empty (it picks the only device) or set the serial from the Device tab's
   *ADB devices* list.
4. Make sure no other adb server on the host grabs the phone (`adb kill-server` on the host).
