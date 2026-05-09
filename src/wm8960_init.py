#!/usr/bin/env python3
"""WM8960 register initialization for flaky I2C bus.

The Allwinner H616's mv64xxx I2C controller has intermittent errors
communicating with the WM8960 codec on I2C bus 3.  The Linux ALSA
driver uses read-modify-write (snd_soc_component_update_bits) which
often fails because the I2C *read* step times out.

This script does blind writes with retries to set up the complete
capture + playback signal path.  It should run after the WM8960 kernel
driver has probed (even if some of its register writes failed) and
before cortex-core opens any audio streams.

Usage:
    sudo python3 wm8960_init.py          # init and exit
    sudo python3 wm8960_init.py --test    # init + 2-second mic test
"""

import os
import struct
import sys
import time

I2C_BUS = 3
WM8960_ADDR = 0x1A
MAX_RETRIES = 10
RETRY_DELAY = 0.05  # 50ms between retries


def i2c_write_reg(fd, reg, value):
    """Write a 9-bit value to a 7-bit WM8960 register.

    WM8960 uses a 2-byte I2C write: [reg<<1 | value>>8, value & 0xFF]
    """
    byte1 = ((reg & 0x7F) << 1) | ((value >> 8) & 0x01)
    byte2 = value & 0xFF
    data = struct.pack("BB", byte1, byte2)
    os.write(fd, data)


def write_reg_retry(fd, reg, value, label=""):
    """Write a register with retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            i2c_write_reg(fd, reg, value)
            tag = f" ({label})" if label else ""
            if attempt > 1:
                print(f"  Reg 0x{reg:02X} = 0x{value:03X}{tag} OK (attempt {attempt})")
            return True
        except OSError:
            time.sleep(RETRY_DELAY)
    tag = f" ({label})" if label else ""
    print(f"  Reg 0x{reg:02X} = 0x{value:03X}{tag} FAILED after {MAX_RETRIES} attempts")
    return False


def init_wm8960():
    """Initialize WM8960 for capture on LINPUT1 + speaker playback."""

    # Open I2C bus (use O_RDWR for ioctl access)
    import fcntl
    fd = os.open(f"/dev/i2c-{I2C_BUS}", os.O_RDWR)
    # Set slave address (I2C_SLAVE_FORCE = 0x0706 to bypass driver lock)
    fcntl.ioctl(fd, 0x0706, WM8960_ADDR)

    ok_count = 0
    fail_count = 0

    def w(reg, value, label=""):
        nonlocal ok_count, fail_count
        if write_reg_retry(fd, reg, value, label):
            ok_count += 1
        else:
            fail_count += 1

    do_reset = "--reset" in sys.argv
    if do_reset:
        print("WM8960 init: software reset...")
        w(0x0F, 0x000, "RESET")
        time.sleep(0.1)
    else:
        print("WM8960 init: patching registers (no reset)...")

    # ── Power Management ─────────────────────────────────────────
    # Power 1: VMIDSEL=01 (50k divider), VREF=1, AINL=1, AINR=1, ADCL=1, ADCR=1
    w(0x19, 0x0FC, "PWR_MGMT_1: VMID+VREF+ADC+AIN")
    time.sleep(0.1)  # Let VMID settle

    # Power 2: DACL=1, DACR=1, LOUT1=1, ROUT1=1, SPKL=1, SPKR=1
    w(0x1A, 0x1F8, "PWR_MGMT_2: DAC+outputs+speakers")

    # Power 3: LMIC=1, RMIC=1, LOMIX=1, ROMIX=1
    w(0x2F, 0x03C, "PWR_MGMT_3: MIC+output_mixers")

    # ── ADC/DAC Control ──────────────────────────────────────────
    # Reg 5: DACMU=0 (unmute DAC — defaults to muted after power-on!)
    w(0x05, 0x000, "ADC_DAC_CTL1: DAC unmuted")

    # ── Clocking ──────────────────────────────────────────────────
    # MCLK = 24 MHz from H616 PWM1 bypass clock on PI11.
    # Use WM8960 PLL to derive SYSCLK from 24 MHz MCLK.
    #
    # PLL math (WM8960 datasheet section 8.2):
    #   PLLPRESCALE=1 → f1 = MCLK/2 = 12 MHz
    #   f2 = f1 × N.K  (PLL VCO output, MUST be 90–100 MHz!)
    #   SYSCLK = f2 / R  where R=4 when SDM=1
    #   Then SYSCLKDIV divides SYSCLK further.
    #
    # Target: SYSCLK = 12.288 MHz (256 × 48000)
    #   With SYSCLKDIV=2: PLL output = 12.288 × 2 = 24.576 MHz
    #   f2 = 24.576 × R = 24.576 × 4 = 98.304 MHz  ✓ (in VCO range)
    #   N.K = 98.304 / 12 = 8.192
    #   N = 8, K = 0.192 × 2^24 = 3221225 = 0x3126E9
    #
    # Reg 4: CLKSEL=1 (PLL), SYSCLKDIV=10 (div 2)
    #   bit 0 = CLKSEL, bits [2:1] = SYSCLKDIV
    w(0x04, 0x005, "CLOCKING_1: PLL source, SYSCLK div 2")

    # Reg 52 (0x34): PLLPRESCALE=1, SDM=1, PLLN=8  → 0x20 | 0x10 | 8 = 0x38
    #   bit 5 = PLLPRESCALE (div 2)
    #   bit 4 = SDM (fractional mode)
    #   bits 3:0 = PLLN
    w(0x34, 0x038, "PLL_N: prescale/2, frac, N=8")
    # Reg 53 (0x35): PLLK[23:16] = 0x31
    w(0x35, 0x031, "PLL_K1: 0x31")
    # Reg 54 (0x36): PLLK[15:8] = 0x26
    w(0x36, 0x026, "PLL_K2: 0x26")
    # Reg 55 (0x37): PLLK[7:0] = 0xE9
    w(0x37, 0x0E9, "PLL_K3: 0xE9")

    # Enable PLL in Power Management 2 (add PLLEN bit 0)
    w(0x1A, 0x1F9, "PWR_MGMT_2: DAC+outputs+speakers+PLL")

    # ── Audio Interface ──────────────────────────────────────────
    # Reg 7: I2S format (10), 32-bit word length (11), slave mode
    # H616 AHUB sends 32-bit I2S slots (slot_width_select=32 in DT)
    w(0x07, 0x00E, "AUDIO_IFACE: I2S 32-bit slave")

    # ── Input Signal Path ─────────────────────────────────────────
    # Reg 32 (0x20): LINPUT1 to PGA (LMN1=1), PGA to boost (LMIC2B=1)
    #   Bit 8: LMN1, Bit 3: LMIC2B = 0x108
    w(0x20, 0x108, "L_INPUT_PATH: LINPUT1→PGA→boost")

    # Reg 33 (0x21): RINPUT1 to PGA (RMN1=1), PGA to boost (RMIC2B=1)
    w(0x21, 0x108, "R_INPUT_PATH: RINPUT1→PGA→boost")

    # ── Input PGA Volume ──────────────────────────────────────────
    # Reg 0: Left Input Volume — IPVU=1, LINMUTE=0, LINVOL=0x17 (0dB default)
    w(0x00, 0x117, "L_INPUT_VOL: 0dB unmuted")

    # Reg 1: Right Input Volume — IPVU=1, RINMUTE=0, RINVOL=0x17
    w(0x01, 0x117, "R_INPUT_VOL: 0dB unmuted")

    # ── Boost Mixer ───────────────────────────────────────────────
    # Reg 43 (0x2B): Left boost mixer — LINPUT1 boost = +20dB (value 2 of 0-3)
    w(0x2B, 0x050, "L_BOOST_MIX: LINPUT1 +20dB")

    # Reg 44 (0x2C): Right boost mixer — RINPUT1 boost = +20dB
    w(0x2C, 0x050, "R_BOOST_MIX: RINPUT1 +20dB")

    # ── ADC Volume ────────────────────────────────────────────────
    # Reg 21 (0x15): Left ADC vol — ADCVU=1, vol=0xC3 (0dB)
    w(0x15, 0x1C3, "L_ADC_VOL: 0dB")

    # Reg 22 (0x16): Right ADC vol
    w(0x16, 0x1C3, "R_ADC_VOL: 0dB")

    # ── DAC Volume (for speaker output) ───────────────────────────
    # Reg 10 (0x0A): Left DAC vol — DACVU=1, vol=0xFF (0dB)
    w(0x0A, 0x1FF, "L_DAC_VOL: 0dB")
    # Reg 11 (0x0B): Right DAC vol
    w(0x0B, 0x1FF, "R_DAC_VOL: 0dB")

    # ── Output Mixer ──────────────────────────────────────────────
    # Reg 34 (0x22): Left output mixer — DAC to mixer (LD2LO=1)
    w(0x22, 0x100, "L_OUT_MIX: DAC→output")
    # Reg 35 (0x23): Right output mixer — DAC to mixer (RD2RO=1)
    w(0x23, 0x100, "R_OUT_MIX: DAC→output")

    # ── Class D Speaker Amplifier ────────────────────────────────
    # Reg 49 (0x31): Enable both L+R Class D speaker outputs
    w(0x31, 0x0F7, "CLASSD_CTL1: enable L+R speaker amp")

    # ── Headphone / Line Output Volume ────────────────────────────
    # Reg 2 (0x02): LOUT1 vol — update=1, vol=0x79 (0dB)
    w(0x02, 0x179, "LOUT1_VOL: 0dB")
    # Reg 3 (0x03): ROUT1 vol
    w(0x03, 0x179, "ROUT1_VOL: 0dB")

    # ── Speaker Volume ────────────────────────────────────────────
    # Reg 40 (0x28): Left speaker vol — SPKVU=1, vol=0x79 (0dB)
    w(0x28, 0x179, "L_SPK_VOL: 0dB")
    # Reg 41 (0x29): Right speaker vol
    w(0x29, 0x179, "R_SPK_VOL: 0dB")

    # ── Additional Control ────────────────────────────────────────
    # Reg 23 (0x17): Additional control — ADCPOL=00, DATSEL=00, TOCLKSEL=0
    w(0x17, 0x1C0, "ADDL_CTRL_1: normal")

    # Reg 24 (0x18): Additional control 2
    w(0x18, 0x000, "ADDL_CTRL_2: defaults")

    # Reg 28 (0x1C): Additional control (this register fails at boot)
    w(0x1C, 0x000, "ADDL_CTRL_4: defaults")

    os.close(fd)

    print(f"\nWM8960 init complete: {ok_count} OK, {fail_count} FAILED")
    return fail_count == 0


def test_mic(duration=2):
    """Record a short clip and report peak/RMS levels."""
    import subprocess
    import wave
    import math

    path = "/tmp/wm8960_test.wav"
    print(f"\nRecording {duration}s from mic...")
    subprocess.run(
        ["arecord", "-D", "plughw:0,0", "-d", str(duration),
         "-f", "S16_LE", "-r", "16000", "-c", "1", path],
        capture_output=True,
    )
    try:
        w = wave.open(path, "rb")
        frames = w.readframes(w.getnframes())
        w.close()
    except Exception as e:
        print(f"Failed to read recording: {e}")
        return False

    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    peak = max(abs(s) for s in samples)
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5

    print(f"  Samples: {len(samples)}")
    print(f"  Peak: {peak} ({20 * __import__('math').log10(peak / 32767):.1f} dBFS)" if peak > 0 else f"  Peak: 0 (SILENT)")
    print(f"  RMS: {rms:.1f}")

    if peak > 500:
        print("  ✓ Microphone is working!")
        return True
    elif peak > 0:
        print("  △ Very low signal — check mic connection")
        return False
    else:
        print("  ✗ No audio captured — mic not working")
        return False


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Error: must run as root (sudo)")
        sys.exit(1)

    success = init_wm8960()

    if "--test" in sys.argv:
        test_mic()

    sys.exit(0 if success else 1)
