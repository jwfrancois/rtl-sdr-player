#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# RTL-SDR Player — one-shot Linux host setup (Ubuntu / Debian).
#
# Fixes the things that stop the player from seeing the dongle:
#   1. the DVB TV kernel driver (dvb_usb_rtl28xxu) claiming the stick
#   2. USB device permissions (udev rules for 0bda:2832 / 0bda:2838)
#   3. libusb1 missing
#
# Run ON THE HOST (also the host side of a Docker deployment - the kernel
# driver and udev rules live outside the container):
#   sudo bash linux-setup.sh
# Then replug the dongle (or reboot) and start the player:
#   bash run.sh            # bare host
#   docker compose up -d   # Docker
# ---------------------------------------------------------------------------
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "please run with sudo:  sudo bash linux-setup.sh"
  exit 1
fi

echo "== RTL-SDR Player: Linux host setup =="

# --- 1. native libraries ----------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq 2>/dev/null || true
  if ! dpkg -s libusb-1.0-0 >/dev/null 2>&1; then
    echo "-- installing libusb-1.0-0"
    apt-get install -y -qq libusb-1.0-0 || \
      echo "   (could not install libusb automatically - install it manually)"
  else
    echo "-- libusb-1.0-0 already installed"
  fi
  # v1.7.5/1.7.6: the player prefers the DISTRO librtlsdr over the Python
  # wheel's bundled snapshot (that one can segfault on modern kernels).
  if dpkg -s librtlsdr0 >/dev/null 2>&1; then
    echo "-- librtlsdr0 already installed"
  else
    echo "-- installing librtlsdr0 + rtl-sdr (distro native library + tools)"
    # rtl-sdr lives in the 'universe' component on Ubuntu; minimal/server
    # images often ship without it, so enable it if apt cannot see the pkg
    if ! apt-get install --dry-run -qq librtlsdr0 >/dev/null 2>&1; then
      echo "-- librtlsdr0 is not visible to apt: enabling Ubuntu 'universe'"
      if command -v add-apt-repository >/dev/null 2>&1; then
        add-apt-repository -y universe >/dev/null 2>&1 || true
      fi
      for f in /etc/apt/sources.list.d/*.sources; do
        # deb822 sources (Ubuntu 24.04): append 'universe' to every
        # Components: line that does not have it yet
        if [ -f "$f" ] && grep -q "^Components:" "$f" \
           && ! grep "^Components:" "$f" | grep -q universe; then
          sed -i '/^Components: /{/universe/!s/^Components: \(.*\)$/Components: \1 universe/}' "$f"
          echo "   (added universe to $f)"
        fi
      done
      apt-get update -qq 2>/dev/null || true
    fi
    if apt-get install -y -qq librtlsdr0 rtl-sdr 2>/dev/null; then
      echo "-- librtlsdr0 + rtl-sdr installed"
    else
      echo "-- apt route failed: downloading the .deb directly"
      tmp=$(mktemp -d)
      URL="http://archive.ubuntu.com/ubuntu/pool/universe/r/rtl-sdr/librtlsdr0_0.6.0-4_amd64.deb"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$tmp/librtlsdr0.deb" "$URL" || true
      elif command -v wget >/dev/null 2>&1; then
        wget -qO "$tmp/librtlsdr0.deb" "$URL" || true
      fi
      if [ -s "$tmp/librtlsdr0.deb" ]; then
        dpkg -i "$tmp/librtlsdr0.deb" || apt-get install -y -f -qq || true
      fi
      rm -rf "$tmp"
      if dpkg -s librtlsdr0 >/dev/null 2>&1; then
        echo "-- librtlsdr0 installed (direct .deb route)"
      else
        echo "   (could not install automatically. On Ubuntu run:"
        echo "    sudo add-apt-repository universe && sudo apt update \\"
        echo "    && sudo apt install librtlsdr0 rtl-sdr)"
      fi
    fi
  fi
else
  echo "-- apt-get not found: make sure libusb-1.0 and librtlsdr are installed"
fi

# --- 2. keep the DVB TV driver away from the dongle ------------------------
BL=/etc/modprobe.d/rtl-sdr-player-blacklist.conf
echo "-- writing $BL (DVB TV driver blacklist)"
cat > "$BL" <<'EOF'
# RTL-SDR Player: stop the DVB TV stack from claiming the RTL2832U dongle.
# The dongle must stay free for librtlsdr (userspace) to open it.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist r820t
EOF

# unload now if loaded (replug after this; reboot not required)
for m in dvb_usb_rtl28xxu rtl2832 r820t; do
  if lsmod | grep -q "^${m} "; then
    echo "-- unloading module $m"
    modprobe -r "$m" 2>/dev/null || rmmod "$m" 2>/dev/null || \
      echo "   (could not unload $m - it will be gone after a reboot)"
  fi
done

# --- 3. udev rules: make the device node world-accessible ------------------
RU=/etc/udev/rules.d/55-rtl-sdr-player.rules
echo "-- writing $RU (udev access rules)"
cat > "$RU" <<'EOF'
# RTL-SDR Player: readable/writable device nodes for RTL2832U dongles
ACTION=="add", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", MODE:="0666"
ACTION=="add", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE:="0666"
EOF

udevadm control --reload-rules 2>/dev/null
udevadm trigger 2>/dev/null

echo
echo "== done. Now: replug the dongle, then start the player. =="
echo "   bare host : bash run.sh"
echo "   Docker    : docker compose up -d --build"
echo "   check     : the web UI shows a dongle diagnostics banner if"
echo "               anything is still wrong (v1.7.2+)."
