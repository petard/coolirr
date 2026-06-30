# coolirr: Moving Optics Diagnostics

`coolirr` is a standalone, cross-platform diagnostic tool for Nikon Coolscan scanners (e.g. LS-50, LS-4000, LS-5000) that runs the moving optics test over USB. It accesses the raw uncalibrated CCD data directly to evaluate optical alignment and LED health.

## Prerequisites

- Python 3.7+

### macOS Setup

On macOS, you need to install the `libusb` dependency. The easiest way is via Homebrew:

```bash
# Install Homebrew if you don't have it (https://brew.sh)
brew install libusb

# Install the Python usb dependency
pip3 install pyusb
```

### Windows Setup

On Windows, Python needs a generic USB driver to communicate with the scanner. The stock Nikon or Vuescan drivers might prevent raw libusb access.

1. Download and run [Zadig](https://zadig.akeo.ie/).
2. Connect and turn on your scanner.
3. In Zadig, select `Options -> List All Devices`.
4. Select your Nikon scanner from the dropdown (e.g., "LS-50 ED").
5. Choose **WinUSB** as the target driver and click **Replace Driver**.
6. Open Command Prompt or PowerShell and install `pyusb`:
   
```cmd
pip install pyusb
```

*(Note: If you want to use the scanner with Nikon Scan or Vuescan again, you will need to uninstall the WinUSB driver from Device Manager and reinstall the original driver).*

## Running Diagnostics

Ensure your MA-21 adapter is inserted and empty. No other adapters are supported by this diagnostic.

Simply run the script:

```bash
python coolirr.py
```

The script will query the scanner, perform a one-line moving scan, and print a table with the metrics for the Red, Green, Blue, and Infrared channels.
