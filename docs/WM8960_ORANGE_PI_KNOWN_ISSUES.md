# WM8960 Audio HAT — Orange Pi Zero 2W Known Issues

## MCLK Pin Mismatch (No Audio Output / No Mic Input)

**Status**: Unresolved — hardware modification required

### Symptom

The WM8960 HAT speaker produces white noise instead of audio, and the microphone captures all zeros. The I2S data path (BCLK, LRCLK, DOUT, DIN) works — playback and recording start/stop correctly — but the WM8960's DAC and ADC never convert because they lack a master clock (MCLK).

### Root Cause

The Waveshare WM8960 Audio HAT was designed for Raspberry Pi, where MCLK is provided on **physical pin 7** (GPIO4/GPCLK0). On the Orange Pi Zero 2W, the I2S MCLK signal (PI0) is on **physical pin 29**, not pin 7.

| Signal | Raspberry Pi | Orange Pi Zero 2W | Match? |
|--------|-------------|-------------------|--------|
| I2S MCLK | Pin 7 (GPIO4/GPCLK0) | **Pin 29** (PI0) | **NO** |
| I2S BCLK | Pin 12 (GPIO18) | Pin 12 (PI1) | Yes |
| I2S LRCLK | Pin 35 (GPIO19) | Pin 35 (PI2) | Yes |
| I2S DOUT | Pin 40 (GPIO21) | Pin 40 (PI3) | Yes |
| I2S DIN | Pin 38 (GPIO20) | Pin 38 (PI4) | Yes |

The HAT's PCB trace routes pin 7 to the WM8960's MCLK input. On the Orange Pi, pin 7 is PI13 (PWM2), which is not an I2S clock. So MCLK never reaches the codec chip.

Without MCLK, the WM8960's sigma-delta DAC and ADC cannot operate. The Class D amplifier still powers on (hence the white noise), but no digital-to-analog conversion occurs.

### I2C Bus 3 Flakiness (Secondary Issue)

The Allwinner H616's mv64xxx I2C controller has intermittent communication errors with the WM8960 on I2C bus 3. The ALSA kernel driver uses read-modify-write (`snd_soc_component_update_bits`) which fails when the I2C read step times out, leaving critical registers unconfigured.

`wm8960_init.py` works around this by doing blind writes with retries. This fixes the register configuration issue but does not solve the MCLK pin mismatch.

### Possible Fixes

1. **Solder jumper wire (recommended)** — Connect physical pin 29 (PI0/MCLK) to the WM8960's MCLK pad on the HAT (or to pin 7 on the header). This is the simplest and most reliable fix.

2. **PWM clock on PI13** — Configure PI13's PWM2 function to output a clock on pin 7. However, the H616's 24 MHz oscillator cannot produce the required 12.288 MHz (256x48kHz) exactly, so audio quality would likely suffer from clock jitter.

3. **Use a different HAT** — Use an I2S audio board designed for the Orange Pi Zero 2W's pin layout, or one that derives MCLK from BCLK internally.

### Files Related to This Issue

- `src/wm8960_init.py` — Register initialization script (works around I2C flakiness, correct register values for 32-bit I2S slave mode)
- Device tree overlay: `sun50i-h616-wm8960.dtbo` — Configures AHUB I2S1 with PI0-PI4 pins, CPU-master mode, 32-bit slots

### Investigation Notes

- The active AHUB I2S port is at register offset 0x300 within the AHUB (base 0x05097000), not 0x000
- MCLK_OUT_EN was confirmed enabled at AHUB register 0x324 — the SoC *is* outputting MCLK on PI0, it just doesn't reach the HAT
- All register configurations in `wm8960_init.py` are verified correct (I2S 32-bit slave, DAC unmuted, Class D amp enabled, input/output paths configured)
- White noise level is identical whether playing silence or a tone — confirms the DAC is not converting (pure amplifier noise)
