#!/usr/bin/env python3
"""Complete audio hardware setup for Orange Pi Zero 2W + WM8960 HAT.

The Allwinner H616 AHUB architecture is missing the sunxi-ahub-daudio
kernel module in the Armbian build.  Without it:
  - No MCLK output (WM8960 needs master clock)
  - No I2S pin mux (BCLK/LRCLK/DOUT/DIN stay at io_disable)
  - No AHUB TDM1 TX enable (audio data never reaches physical output)
  - No WM8960 register init (kernel driver I2C writes fail)

This script manually configures everything via /dev/mem register writes
and sysfs.  It must run as root after boot, before cortex-core.

Hardware wiring:
  PI11 (PWM1 bypass 24MHz) ──wire──> PI13 (HAT pin 7) > WM8960 MCLK
  PI1-PI4 (i2s0 function)  ──HAT──> WM8960 BCLK/LRCLK/DOUT/DIN
  I2C-3                     ──HAT──> WM8960 control (addr 0x1A)

Usage:
    sudo python3 audio_setup.py          # full setup
    sudo python3 audio_setup.py --test   # setup + play test tone
"""

import fcntl
import mmap
import os
import struct
import subprocess
import sys
import time

# ── Constants ────────────────────────────────────────────────────────

PIO_BASE    = 0x0300B000   # GPIO pin controller
PWM_BASE    = 0x0300A000   # PWM controller
AHUB_BASE   = 0x05097000   # Audio Hub
I2C_BUS     = 3
WM8960_ADDR = 0x1A
MAX_RETRIES = 10
RETRY_DELAY = 0.05


# ── Low-level helpers ────────────────────────────────────────────────

def devmem_open():
    """Open /dev/mem and return fd."""
    return os.open("/dev/mem", os.O_RDWR | os.O_SYNC)


def reg_read(mm, offset):
    return struct.unpack("<I", mm[offset:offset+4])[0]


def reg_write(mm, offset, value):
    mm[offset:offset+4] = struct.pack("<I", value)


def reg_set_field(mm, offset, shift, width, value):
    """Read-modify-write a bit field."""
    mask = ((1 << width) - 1) << shift
    old = reg_read(mm, offset)
    new = (old & ~mask) | ((value << shift) & mask)
    reg_write(mm, offset, new)
    return new


# ── Step 1: PWM1 bypass on PI11 for 24 MHz MCLK ────────────────────

def setup_mclk(fd):
    """Enable PWM1 bypass output on PI11 to generate 24 MHz MCLK."""
    print("Step 1: MCLK (PWM1 bypass on PI11)")

    # 1a. Export PWM channel 1 via sysfs (ignore error if already exported)
    try:
        with open("/sys/class/pwm/pwmchip0/export", "w") as f:
            f.write("1")
    except OSError:
        pass  # already exported

    # Set period/duty and enable (needed before bypass register works)
    for path, val in [
        ("/sys/class/pwm/pwmchip0/pwm1/period", "1000"),
        ("/sys/class/pwm/pwmchip0/pwm1/duty_cycle", "500"),
        ("/sys/class/pwm/pwmchip0/pwm1/enable", "1"),
    ]:
        try:
            with open(path, "w") as f:
                f.write(val)
        except OSError as e:
            print(f"  Warning: {path}: {e}")

    # 1b. Set bypass mode via register (Ch1 PCR at PWM_BASE+0x080)
    #     Bit 9 = CLK_BYPASS, Bit 8 = SCLK_GATING
    mm = mmap.mmap(fd, 0x400, offset=PWM_BASE)
    ch1_pcr = reg_read(mm, 0x080)
    ch1_pcr |= 0x300  # bypass + sclk gate
    reg_write(mm, 0x080, ch1_pcr)
    actual = reg_read(mm, 0x080)
    mm.close()

    if actual & 0x200:
        print("  PWM1 bypass ACTIVE (24 MHz on PI11)")
    else:
        print("  WARNING: bypass bit did not stick (0x%08X)" % actual)

    # 1c. Force PI11 to PWM function 5
    mm = mmap.mmap(fd, 0x200, offset=PIO_BASE)
    reg_set_field(mm, 0x124, 12, 4, 5)  # PI11 = bits [15:12] of PI_CFG1

    # 1d. Set PI13 to INPUT (function 0) — it's wired to PI11, must not drive
    reg_set_field(mm, 0x124, 20, 4, 0)  # PI13 = bits [23:20] of PI_CFG1

    pi11 = (reg_read(mm, 0x124) >> 12) & 0xF
    pi13 = (reg_read(mm, 0x124) >> 20) & 0xF
    print(f"  PI11=func{pi11}(want 5/PWM)  PI13=func{pi13}(want 0/input)")
    mm.close()


# ── Step 2: I2S pin mux ─────────────────────────────────────────────

def setup_i2s_pins(fd):
    """Set PI1-PI4 to i2s0 function (function 2) for BCLK/LRCLK/DOUT/DIN."""
    print("Step 2: I2S pins (PI1-PI4 → i2s0)")

    mm = mmap.mmap(fd, 0x200, offset=PIO_BASE)
    for pin in [1, 2, 3, 4]:
        reg_set_field(mm, 0x120, pin * 4, 4, 2)  # PI_CFG0, function 2

    cfg0 = reg_read(mm, 0x120)
    mm.close()
    for pin in [1, 2, 3, 4]:
        f = (cfg0 >> (pin * 4)) & 0xF
        names = {1: "BCLK", 2: "LRCLK", 3: "DOUT", 4: "DIN"}
        print(f"  PI{pin}({names[pin]})=func{f}(want 2/i2s0)")


# ── Step 3: AHUB TDM1 enable ────────────────────────────────────────

def setup_ahub_tdm1(fd):
    """Configure AHUB TDM1 for I2S TX output."""
    print("Step 3: AHUB TDM1 (TX enable, unmute, SDO0)")

    mm = mmap.mmap(fd, 0x1000, offset=AHUB_BASE)

    # Read current I2S_CTL
    ctl = reg_read(mm, 0x300)
    print(f"  TDM1 I2S_CTL before: 0x{ctl:08X}")

    # Set: GEN=1(b0), TXEN=1(b2), MODE=1(b4), SDO0_EN=1(b8), CLK_OUT=1(b18)
    # Clear OUT_MUTE(b6)
    ctl |= (1 << 0) | (1 << 2) | (1 << 4) | (1 << 8) | (1 << 18)
    ctl &= ~(1 << 6)  # unmute
    reg_write(mm, 0x300, ctl)
    print(f"  TDM1 I2S_CTL after:  0x{reg_read(mm, 0x300):08X}")

    # Channel config: 2ch TX, 2ch RX
    reg_write(mm, 0x324, 0x00000011)

    # Verify routing
    rxcont = reg_read(mm, 0x320)
    fmt0 = reg_read(mm, 0x304)
    clkd = reg_read(mm, 0x30C)
    print(f"  RXCONT=0x{rxcont:08X} FMT0=0x{fmt0:08X} CLKD=0x{clkd:08X}")

    mm.close()


# ── Step 4: WM8960 register init ────────────────────────────────────

def setup_wm8960():
    """Initialize WM8960 codec registers via I2C blind writes."""
    print("Step 4: WM8960 codec init")

    fd = os.open(f"/dev/i2c-{I2C_BUS}", os.O_RDWR)
    fcntl.ioctl(fd, 0x0706, WM8960_ADDR)

    ok = 0
    fail = 0

    def w(reg, value, label=""):
        nonlocal ok, fail
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                b1 = ((reg & 0x7F) << 1) | ((value >> 8) & 0x01)
                b2 = value & 0xFF
                os.write(fd, struct.pack("BB", b1, b2))
                if attempt > 1:
                    print(f"  0x{reg:02X}=0x{value:03X} ({label}) OK attempt {attempt}")
                ok += 1
                return
            except OSError:
                time.sleep(RETRY_DELAY)
        print(f"  0x{reg:02X}=0x{value:03X} ({label}) FAILED")
        fail += 1

    # Software reset
    w(0x0F, 0x000, "RESET")
    time.sleep(0.1)

    # ── Power ──
    w(0x19, 0x0FC, "PWR1: VMID+VREF+ADC+AIN")
    time.sleep(0.1)
    w(0x1A, 0x1F8, "PWR2: DAC+outputs+speakers")
    w(0x2F, 0x03C, "PWR3: MIC+mixers")

    # ── DAC unmute ──
    w(0x05, 0x000, "DAC unmute")

    # ── Clocking (PLL from 24 MHz MCLK → 12.288 MHz SYSCLK) ──
    w(0x04, 0x005, "CLK1: PLL, SYSCLKDIV=2")
    w(0x34, 0x038, "PLL_N: prescale/2, frac, N=8")
    w(0x35, 0x031, "PLL_K1")
    w(0x36, 0x026, "PLL_K2")
    w(0x37, 0x0E9, "PLL_K3")
    w(0x1A, 0x1F9, "PWR2+PLL")

    # ── Audio interface: I2S, 32-bit, slave ──
    # NOTE: AHUB TDM1 MODE=1. sunxi MODE=1 = I2S (not left-justified).
    # WM8960 fmt bits[1:0]: 00=right-j, 01=left-j, 10=I2S, 11=PCM
    w(0x07, 0x00E, "IFACE: I2S 32-bit slave")

    # ── Input path ──
    w(0x20, 0x108, "L_IN: LINPUT1→PGA→boost")
    w(0x21, 0x108, "R_IN: RINPUT1→PGA→boost")
    w(0x00, 0x117, "L_IN_VOL: 0dB")
    w(0x01, 0x117, "R_IN_VOL: 0dB")
    w(0x2B, 0x050, "L_BOOST: +20dB")
    w(0x2C, 0x050, "R_BOOST: +20dB")
    w(0x15, 0x1C3, "L_ADC_VOL: 0dB")
    w(0x16, 0x1C3, "R_ADC_VOL: 0dB")

    # ── Output path ──
    w(0x0A, 0x1FF, "L_DAC_VOL: 0dB")
    w(0x0B, 0x1FF, "R_DAC_VOL: 0dB")
    w(0x22, 0x100, "L_OUT_MIX: DAC→out")
    w(0x23, 0x100, "R_OUT_MIX: DAC→out")
    w(0x31, 0x0F7, "CLASSD: L+R speaker amp")
    w(0x02, 0x179, "LOUT1: 0dB")
    w(0x03, 0x179, "ROUT1: 0dB")
    w(0x28, 0x179, "L_SPK: 0dB")
    w(0x29, 0x179, "R_SPK: 0dB")

    # ── Additional control ──
    w(0x17, 0x1C0, "ADDL1: normal")
    w(0x18, 0x000, "ADDL2: defaults")
    w(0x1C, 0x000, "ADDL4: defaults")

    os.close(fd)
    print(f"  WM8960: {ok} OK, {fail} FAILED")
    return fail == 0


# ── Step 5: Test ─────────────────────────────────────────────────────

def test_playback():
    """Play a test tone to verify audio output."""
    print("\nStep 5: Playing 440 Hz test tone for 2 seconds...")
    try:
        subprocess.run(
            ["speaker-test", "-D", "plughw:wm8960soundcard",
             "-t", "sine", "-f", "440", "-r", "48000", "-l", "1"],
            timeout=5,
            capture_output=True,
        )
        print("  Playback complete — did you hear a tone?")
    except subprocess.TimeoutExpired:
        print("  Playback timed out (normal for speaker-test)")
    except FileNotFoundError:
        # Fall back to aplay
        sounds = "/home/turfptax/cortex-core/assets/sounds"
        wav = os.path.join(sounds, "boot.wav")
        if os.path.exists(wav):
            subprocess.run(["aplay", "-D", "plughw:wm8960soundcard", wav])
            print("  Played boot.wav")
        else:
            print("  No test audio available")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    if os.geteuid() != 0:
        print("Error: must run as root (sudo)")
        return False

    print("=" * 60)
    print("Audio Setup: Orange Pi H616 + WM8960 HAT")
    print("=" * 60)

    fd = devmem_open()

    setup_mclk(fd)
    print()
    setup_i2s_pins(fd)
    print()
    setup_ahub_tdm1(fd)
    print()

    os.close(fd)

    success = setup_wm8960()
    print()

    if "--test" in sys.argv:
        test_playback()

    print("=" * 60)
    print("Audio setup complete." + (" All WM8960 writes OK." if success else " Some WM8960 writes FAILED."))
    print("=" * 60)
    return success


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
