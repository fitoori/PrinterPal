# PrinterPal

PrinterPal is a Flask web UI for a Raspberry Pi print server using CUPS.

## Install (Debian / Raspberry Pi OS)
Run as root:

```bash
cd PrinterPal
chmod +x install.sh
sudo ./install.sh
```

After install, open:
- http://<pi-ip>/

## Service management
```bash
sudo systemctl status printerpal
sudo systemctl restart printerpal
sudo journalctl -u printerpal -f
```

## Files and config
- Uploads: /var/lib/printerpal/uploads
- Preview cache: /var/lib/printerpal/cache
- Config: /etc/printerpal/config.json
