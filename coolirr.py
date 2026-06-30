#!/usr/bin/env python3
import usb.core
import usb.util
import struct
import time
import sys

class CoolscanError(Exception):
    pass

class SenseData:
    def __init__(self, raw):
        if len(raw) < 4:
            self.key = 0; self.asc = 0; self.ascq = 0
            return
        self.key = raw[1]
        self.asc = raw[2]
        self.ascq = raw[3]

class NikonUSBTransport:
    PHASE_QUERY = 0xD0

    def __init__(self):
        self.dev = usb.core.find(idVendor=0x04b0)
        if self.dev is None:
            raise CoolscanError("No Nikon scanner found (vendor 0x04b0). Please check USB and drivers.")
        
        try:
            self.dev.set_configuration()
        except Exception:
            pass

        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except Exception:
            pass

        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)

        if not self.ep_out or not self.ep_in:
            raise CoolscanError("Could not find bulk in/out endpoints")

    def execute(self, cdb, data_out=None, data_in_len=0, is_polling=False):
        self.ep_out.write(cdb)
        self.ep_out.write(bytes([self.PHASE_QUERY]))
        
        phase = self.ep_in.read(1, timeout=5000)
        if len(phase) == 0:
            raise CoolscanError("No phase byte")
        phase_byte = phase[0]

        received = None
        if phase_byte == 0x03:
            if data_in_len > 0:
                received = self.read_chunked(data_in_len)
        elif phase_byte == 0x02:
            if data_out:
                self.ep_out.write(data_out)
        
        sense_raw = self.ep_in.read(8, timeout=5000)
        return received, SenseData(sense_raw)

    def read_chunked(self, total):
        buffer = bytearray()
        while len(buffer) < total:
            try:
                chunk = self.ep_in.read(total - len(buffer), timeout=5000)
                if not chunk: break
                buffer.extend(chunk)
            except usb.core.USBError:
                break
        return bytes(buffer)

def decode_status(sense):
    if sense.key == 0x00: return "ready"
    if sense.key == 0x02:
        return "processing" if sense.asc == 0x04 else ("noDocs" if sense.asc == 0x3a else "error")
    if sense.key == 0x09 or sense.key == 0x06: return "reissue"
    return "error"

def execute_with_retry(transport, cdb, data_out=None, data_in_len=0):
    retry = 3
    while retry > 0:
        received, sense = transport.execute(cdb, data_out=data_out, data_in_len=data_in_len)
        st = decode_status(sense)
        if st == "reissue":
            wait_ready(transport)
            retry -= 1
            continue
        if st == "error":
            raise CoolscanError(f"SCSI Error: Key: {sense.key:02X}, ASC: {sense.asc:02X}, ASCQ: {sense.ascq:02X}")
        return received, sense
    raise CoolscanError("Command failed after retries")

def wait_ready(transport, timeout=120):
    start = time.time()
    first = True
    while time.time() - start < timeout:
        if not first:
            time.sleep(1.0)
        first = False
        _, sense = transport.execute(bytes([0x00, 0, 0, 0, 0, 0]), is_polling=True)
        st = decode_status(sense)
        if st in ["ready", "noDocs"]:
            return st
    raise CoolscanError("wait_ready timeout")

def be16(val): return struct.pack('>H', val & 0xFFFF)
def be32(val): return struct.pack('>I', val & 0xFFFFFFFF)
def parse_be16(data, offset): return struct.unpack('>H', data[offset:offset+2])[0]
def parse_be32(data, offset): return struct.unpack('>I', data[offset:offset+4])[0]

def run_inquiry(transport):
    basic, _ = execute_with_retry(transport, bytes([0x12, 0x00, 0x00, 0x00, 36, 0x00]), data_in_len=36)
    product = basic[16:32].decode('ascii', errors='ignore').strip()
    
    head, _ = execute_with_retry(transport, bytes([0x12, 0x01, 0xC1, 0x00, 0x04, 0x00]), data_in_len=4)
    total = head[3] + 4
    page, _ = execute_with_retry(transport, bytes([0x12, 0x01, 0xC1, 0x00, total, 0x00]), data_in_len=total)
    
    maxBits = page[82]
    if "LS-40 " in product:
        maxBits = 10

    info = {
        'product': product,
        'maxBits': maxBits,
        'resxMax': parse_be16(page, 20),
        'resyMax': parse_be16(page, 42),
        'boundaryX': parse_be32(page, 36),
        'boundaryY': parse_be32(page, 58),
        'adapterFrameCount': page[75]
    }
    
    adapter = "MA-21"
    if info['boundaryY'] >= 5900 or info['adapterFrameCount'] >= 2: adapter = "SA-21"
    elif info['boundaryX'] > 0 and info['boundaryX'] < 3400: adapter = "IA-20"
    
    info['adapter'] = adapter
    info['calibrationFocus'] = 186 if adapter == "MA-21" else 323
    return info

def mode_select_and_reserve(transport, info):
    wait_ready(transport)
    dpi = info['resxMax']
    payload = bytes([
        0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x01, 0x03, 0x06, 0x00, 0x00,
        (dpi >> 8) & 0xFF, dpi & 0xFF, 0x00, 0x00
    ])
    execute_with_retry(transport, bytes([0x15, 0x10, 0x00, 0x00, 0x14, 0x00]), data_out=payload)
    execute_with_retry(transport, bytes([0x16, 0, 0, 0, 0, 0]))

def issue_and_execute(transport, cdb, data_out):
    execute_with_retry(transport, cdb, data_out=data_out)
    wait_ready(transport)
    execute_with_retry(transport, bytes([0xC1, 0, 0, 0, 0, 0]))

def configure_moving_optics_base(transport, info):
    wait_ready(transport)
    frame_count = max(1, info['adapterFrameCount'])
    frame_offset = int(info['resyMax'] * 1.5 + 1.0)
    boundary_length = 4 + frame_count * 16
    
    cdb_bound = bytes([0x2A, 0x00, 0x88, 0x00, 0x00, 0x03]) + be32(boundary_length)[1:] + bytes([0x00])
    data_bound = be16(boundary_length) + bytes([frame_count, frame_count])
    for i in range(frame_count):
        y_start = frame_offset * i
        data_bound += be32(y_start) + be32(0) + be32(y_start) + be32(info['boundaryX'] - 1)
    
    execute_with_retry(transport, cdb_bound, data_out=data_bound)
    wait_ready(transport)
    
    focus_data = bytes([0x00]) + be32(info['calibrationFocus']) + bytes([0, 0, 0, 0])
    issue_and_execute(transport, bytes([0xE0, 0x00, 0xC1, 0, 0, 0, 0, 0, 0x09, 0x00]), data_out=focus_data)

def configure_moving_optics_windows(transport, info):
    scan_width = min(3964, info['boundaryX'])
    colors = [1, 2, 3, 9] # Red, Green, Blue, IR
    bright_exp = 120000
    
    for c in colors:
        wait_ready(transport)
        payload = bytes([0, 0, 0, 0, 0, 0, 0, 0x32, c, 0x00])
        payload += be16(info['resxMax']) + be16(info['resyMax'])
        payload += be32(0) + be32(0) + be32(scan_width) + be32(1)
        payload += bytes([0x00, 0x00, 0x00, 0x05, info['maxBits']])
        payload += bytes(13)
        payload += bytes([0x00, 0x81, 0x01, 0x02, 0x02, 0xFF])
        
        if c == 9:
            payload += bytes([0, 0, 0, 0])
        else:
            payload += be32(bright_exp)
            
        execute_with_retry(transport, bytes([0x24, 0, 0, 0, 0, 0, 0, 0, 0x3A, 0x80]), data_out=payload)
    return scan_width

def decode_line(raw, width, maxBits, oddPadding):
    r_pix, g_pix, b_pix, ir_pix = [], [], [], []
    for i in range(width):
        r_off = 2 * (0 * width + i)
        g_off = 2 * (1 * width + i)
        b_off = 2 * (2 * width + i)
        ir_off = 2 * (3 * width + i)
        
        r_pix.append(parse_be16(raw, r_off))
        g_pix.append(parse_be16(raw, g_off))
        b_pix.append(parse_be16(raw, b_off))
        
        if ir_off + 2 <= len(raw):
            ir_pix.append(parse_be16(raw, ir_off))
            
    return [
        ("Red", r_pix), ("Green", g_pix), ("Blue", b_pix), ("Infrared", ir_pix)
    ]

def calc_metrics(pixels, maxBits):
    maxValue = max(pixels)
    minValue = min(pixels)
    
    irregularity = ((maxValue - minValue) / maxValue) * 100.0 if maxValue > 0 else 0.0
    
    edgeCount = max(1, len(pixels) // 5)
    leftAvg = sum(pixels[:edgeCount]) / edgeCount if edgeCount > 0 else 0
    rightAvg = sum(pixels[-edgeCount:]) / edgeCount if edgeCount > 0 else 0
    slope = (rightAvg - leftAvg) / maxValue if maxValue > 0 else 0.0
    
    maxPossibleVal = (1 << maxBits) - 1
    targetPeak = 0.90 * maxPossibleVal
    offTarget = ((maxValue - targetPeak) / targetPeak) * 100.0 if targetPeak > 0 else 0.0
    
    minimumSignal = max(500.0, maxPossibleVal * 0.02)
    hasUsableSignal = maxValue >= minimumSignal
    tiltWithinTolerance = abs(slope) <= 0.05
    passed = hasUsableSignal and irregularity <= 42.0 and tiltWithinTolerance
    
    return maxValue, minValue, offTarget, irregularity, passed

def run_diagnostics():
    try:
        t = NikonUSBTransport()
        print("Connected to Nikon Coolscan.")
        
        st = wait_ready(t)
        if st != "noDocs":
            print("Please remove any film/document from the adapter.")
        
        info = run_inquiry(t)
        if info['adapter'] != "MA-21":
            print(f"Warning: Diagnostics designed for MA-21. Found: {info['adapter']}")
            
        print("Initializing scanner mode (Mode Select / Reserve)...")
        mode_select_and_reserve(t, info)
            
        print("Configuring moving optics base (Focus & Boundaries)...")
        configure_moving_optics_base(t, info)
        
        print("Configuring moving optics exposures...")
        scan_width = configure_moving_optics_windows(t, info)
        
        print("Scanning...")
        wait_ready(t)
        color_list = bytes([1, 2, 3, 9])
        execute_with_retry(t, bytes([0x1B, 0, 0, 0, 0x04, 0x00]), data_out=color_list)
            
        wait_ready(t)
        oddPadding = 0
        transferLength = 4 * scan_width * 2
        
        # Align to 512 for LS-50 / LS-5000 models
        is_5000_series = ("LS-50 " in info['product']) or ("LS-5000" in info['product'])
        if is_5000_series and (transferLength % 512 != 0):
            transferLength = ((transferLength // 512) + 1) * 512
            
        readCDB = bytes([0x28, 0x00, 0x00, 0x00, 0x00, 0x00]) + be32(transferLength)[1:] + bytes([0x00])
        raw, _ = execute_with_retry(t, readCDB, data_in_len=transferLength)
        
        if not raw or len(raw) < (4 * scan_width * 2):
            raise CoolscanError(f"Failed to read image data (read {len(raw) if raw else 0} bytes)")
            
        metrics = decode_line(raw, scan_width, info['maxBits'], oddPadding)
        
        print("\nDiagnostics Complete. Moving Optics Block Results:")
        print("=" * 72)
        print(f"{'Channel':<10} | {'Max':<6} | {'Min':<6} | {'Peak Ref':<12} | {'Irregularity':<12} | {'Result':<6}")
        print("-" * 72)
        
        for name, pixels in metrics:
            if not pixels: continue
            mx, mn, offT, irreg, passed = calc_metrics(pixels, info['maxBits'])
            res_str = "PASS" if passed else "FAIL"
            print(f"{name:<10} | {mx:<6} | {mn:<6} | {offT:>9.1f}% | {irreg:>10.1f}% | {res_str:<6}")
            
        print("=" * 72)
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_diagnostics()
