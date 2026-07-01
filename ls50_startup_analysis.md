# Nikon Coolscan LS-50 Firmware: Hardware Tests & Diagnostics

Based on a deep reverse engineering analysis of the disassembled firmware (`ls50_full.asm`) and context from the `Nikon-Coolscan-RE` repository, here is a comprehensive breakdown of all hardware tests performed by the firmware, as well as the specific failure conditions that trigger the Fast Blinking Light.

## 1. The "Fast Blinking Light" Error Loop (`0x020784`)

When users experience a rapid, continuous flashing of the front LED on power-up (and the scanner fails to appear over USB), it is because the firmware has entered a specific fatal error handler at address `0x020784`.

**Behavior of the Error Loop:**
1. **Interrupts Disabled:** The first instruction in this block is `ORC #0x80:8, CCR`, setting the Interrupt Mask bit. This completely disables all hardware interrupts, leaving the scanner entirely "dead" to the host computer (USB/SCSI).
2. **Tight Delay Loop:** It enters a tight software loop with a hardcoded delay count of 26,041 (`0x000065B9` in `ER3`).
3. **LED Toggle:** At the end of each delay, it manually flips bit 0 of the memory-mapped register at `0xFFFFD7` (the front panel LED) using a `BST/BIST` instruction.
4. **Watchdog Feed:** It continuously resets the hardware Watchdog Timer (`MOV.W #0x5A00, @0xFFA8`) to prevent the system from rebooting.

Because this loop runs at full CPU speed with no interruptions, the resulting blink rate is very fast (3–5 Hz), distinguishing it from the slower (~1 Hz) blink rate driven by normal timer interrupts during a healthy warmup.

### The Trigger: Memory (RAM) Integrity Tests
Analysis of the code paths leading to `0x020784` reveals that **the Fast Blinking Light is exclusively triggered by a Memory (RAM) Test failure.** 

Mechanical errors (like a jammed motor or failed sensor) happen later in the boot process. Because RAM is functioning in those scenarios, the scanner remains alive and reports those mechanical errors cleanly over USB via standard SCSI Sense Codes. But if the RAM itself is faulty, the CPU cannot safely initialize the USB stack.

The RAM test runs in two specific scenarios:
1. **Power-On Self-Test (POST):** During the initial boot (`0x020334`), the firmware runs a destructive bit-walking test on critical RAM. It writes the pattern `0x55AA55AA` (binary `01010101...`), verifies it, then writes `0xAA55AA55` (binary `10101010...`) and verifies it. If this fails, the error register `R2` is set to `0x0010`, and the code branches unconditionally to the fast blink loop.
2. **Host-Requested Test:** The firmware contains a callable version (`sub_020624`) that runs the same checkerboard test across 6 extended memory regions. The host computer triggers this by issuing a SCSI `SEND DIAGNOSTIC` command with the `SelfTest=1` bit set. If any region fails, it branches to `0x020738`, which trampolines straight to the fast blink loop.

---

## 2. Power-On Self Test (POST) Sequence

If the internal RAM test passes, the firmware boots the OS and sequentially tests the physical electro-mechanical hardware. This sequence is strictly ordered. A failure in any phase aborts the rest of the sequence, leaving the scanner initialized via USB but trapped in an error state.

### Phase 1: Adapter Detection (`adapter_detect_and_init` ~ `0x029F80`)
Before moving anything, the scanner must know what is attached.
- **Hardware Logic ID Check:** It reads Port C (`REG_PCDR` at `0xFF92`), testing pins PC3, PC4, and PC5. These form a 3-bit logic ID representing the physical adapter inserted (e.g., MA-21, SA-21).
- **Transport Sensor Polling:** It sequentially reads four distinct optical photointerrupters (MSW5, MSW9, MSW10, MSW11) by triggering the Analog-to-Digital Converter. This verifies the physical film path is clear of obstructions before firing the motors.

### Phase 2: Transport Motor Homing (`sub_016030` sequence)
Once the adapter is identified, the firmware tests the main film transport stepper motor.
- It drives the stepper motor to find the horizontal "home" position while continuously polling the centre sprocket photointerrupter (`REG_ADDRA`). 
- It verifies physical movement by looking for transitions in the optical sensor ADC sample. If the optical state does not change within a hardcoded timeout (e.g., 600ms or 1200ms depending on the adapter), a motor-jam error is flagged.

### Phase 3: Optical Path & LED Calibration (AGC) (`sub_02A812` / `sub_02B3E4`)
The scanner calibrates its imaging sensors to the current ambient temperature and LED brightness.
- **Light Firing & Capture:** It turns the RGB LEDs on full blast. The linear CCD captures the bare light through the scanner's internal calibration slot.
- **AGC Calculation:** The firmware uses heavy integer math (`DIVXU`/`MULXU`) to analyze the CCD data and calculate the Automatic Gain Control (AGC) offsets required for pure white balance. 
- *Failure Point:* If the LEDs are heavily degraded, the mirror is dirty, or the CCD is failing, the algorithms cannot achieve the target brightness. The test fails here.

### Phase 4: Autofocus Motor Homing (Vertical Movement)
*Only if the light calibration succeeds* does the firmware test the vertical lens carriage. This is a **closed-loop mechanical verification test**.
- **Motor & Drive Train Integrity (The Timeout Test):** The firmware pulses the autofocus stepper motor to move the head upwards. It expects the scan head to physically break the beam of the vertical optical limit switch within a hardcoded number of steps. If it hits the step limit (timeout) without the switch triggering, it assumes the motor is jammed, unplugged, or the limit switch is dead.
- **Zero-Point Calibration:** Once the limit switch is successfully triggered, the firmware sets this coordinate as the absolute "zero point" for the lens focal range. It then steps backward slightly to ensure the switch cleanly un-triggers (verifying the switch isn't mechanically stuck).

---

## 3. Reading Exact Failure Points over USB

### Status: I haven't found a path to read the POST results from the scanner. According to Claude, on mac the status is cleared after macOS detects the device.

Because failures in phases 1–4 happen *after* RAM is initialized, the scanner is fully capable of speaking USB. The firmware halts the mechanical initialization but remains alive on the bus. 

You **can** read out the exact failure point over USB by issuing a SCSI `REQUEST SENSE` command (0x03). The Nikon Coolscan uses a proprietary 32-byte sense buffer that exposes highly granular error codes directly from the firmware's internal state machine:

| Component Failure | SCSI Sense Key (SK) | Additional Sense Code (ASC) | Meaning |
| :--- | :--- | :--- | :--- |
| **Autofocus/Lens** | `0x01` (RECOVERED ERROR) | `0x61` | Out of focus (FRU byte identifies specific CCD channel). Often thrown if Phase 4 fails to find the limit switch. |
| **LED / Lamp** | `0x04` (HARDWARE ERROR) | `0x60` | Lamp failure. There are 30 different variants of this code (differentiated by the FRU byte) covering R, G, B, and IR channel dropouts during Phase 3 AGC calibration. |
| **Transport Motor** | `0x04` (HARDWARE ERROR) | `0x53` | Media load/eject failed (Motor jammed or failed to home during Phase 2). |
| **Adapter/Sensor** | `0x02` (NOT READY) | `0x3A` | Medium not present. Firmware cannot detect the adapter or the film path sensors are blocked (Phase 1). |
| **General HW Error** | `0x04` (HARDWARE ERROR) | `0x44` | Internal Target Failure (Generic fatal hardware error). |
| **Vendor Motor Error** | `0x09` (VENDOR SPECIFIC) | `0x80` | Motor control error (ASCQ = `0x01`, FRU = `0x06`). |

By writing a simple USB script (or using NikonScan's diagnostic logs), you can poll the `REQUEST SENSE` buffer and pinpoint exactly which electro-mechanical component caused the POST to halt.
