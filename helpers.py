"""
helpers.py - Helper functions and classes for OpenWrt roaming configuration
"""

import paramiko
import re
import json
from typing import Dict, List, Any, Tuple, Optional
from dataclasses import dataclass, asdict


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class APInterface:
    """Represents a wireless AP interface"""
    phy: str
    interface: str
    mac: str
    ssid: str
    channel: int
    frequency: int
    width: int
    
    def is_24ghz(self) -> bool:
        return 2401 <= self.frequency <= 2495
    
    def is_5ghz(self) -> bool:
        return 5170 <= self.frequency <= 5835
    
    def dump(self) -> Dict[str, Any]:
        """Gibt die Interface-Daten als Dictionary zurück"""
        return asdict(self)


@dataclass
class APInfo:
    """Complete information about an access point"""
    hostname: str
    interface_24: Optional[APInterface] = None
    interface_5: Optional[APInterface] = None

    def dump(self) -> Dict[str, Any]:
        """Gibt die gesamten AP-Daten (inklusive Interfaces) als Dictionary zurück"""
        return asdict(self)


# ============================================================================
# Generic iw Parser
# ============================================================================

def get_indent(line: str) -> int:
    """Count leading spaces"""
    return len(line) - len(line.lstrip())


def parse_iw(lines: List[str], start_idx: int = 0, base_indent: int = 0) -> Tuple[Dict[str, Any], int]:
    """
    Generic recursive parser for iw command output based on indentation
    
    Returns:
        (parsed_dict, next_line_index)
    """
    result = {}
    i = start_idx
    
    while i < len(lines):
        line = lines[i]

        # Skip empty lines
        if not line.strip():
            i += 1
            continue
        
        current_indent = get_indent(line)

        # If less indented than base, we're done with this block
        if current_indent < base_indent:
            break

        # Only process lines at our indent level
        if current_indent > base_indent:
            i += 1
            continue
        
        stripped = line.strip()

        # Check if next line is more indented (nested structure)
        if i + 1 < len(lines):
            next_indent = get_indent(lines[i + 1])
            
            if next_indent > current_indent:
                # This is a parent key, parse children recursively
                key = stripped
                # print(f"key {key}")
                child_dict, next_i = parse_iw(lines, i + 1, next_indent)
                result[key] = child_dict
                i = next_i
                continue

        # Regular key-value line: split at first space
        parts = stripped.split(None, 1)
        if len(parts) == 2:
            key, value = parts
            result[key] = value
        elif len(parts) == 1:
            # Key without value
            result[parts[0]] = None
        
        i += 1
    
    return result, i


def parse_channel_line(channel_str: str) -> Tuple[int, int, int]:
    """
    Parse channel line like: "11 (2462 MHz), width: 20 MHz, center1: 2462 MHz"

    Returns:
        (channel, frequency, width)
    """
    match = re.match(r'(\d+)\s+\((\d+)\s+MHz\).*width:\s+(\d+)', channel_str)
    if match:
        return (
            int(match.group(1)),  # channel
            int(match.group(2)),  # frequency
            int(match.group(3))   # width
        )
    return None, None, None


def parse_iw_dev(output: str) -> List[APInterface]:
    """
    Parse 'iw dev' output into structured APInterface objects
    """
    lines = output.split('\n')
    parsed, _ = parse_iw(lines)
    
    interfaces = []

    # Iterate through phys
    for phy_key, phy_data in parsed.items():
        if not phy_key.startswith('phy#'):
            continue
        
        phy = phy_key.split('#')[1]

        # Iterate through interfaces in this phy
        for iface_key, iface_data in phy_data.items():
            # print(f"parse_iw_dev {iface_key}")
            if not iface_key.startswith('Interface'):
                continue
            
            iface_name = iface_key.split()[1]

            # Check if this is an AP with SSID
            if iface_data.get('type') != 'AP' or 'ssid' not in iface_data:
                continue

            # Parse channel info
            channel_str = iface_data.get('channel')
            if not channel_str:
                continue
            
            channel, frequency, width = parse_channel_line(channel_str)
            if channel is None:
                continue

            # Create APInterface object
            interfaces.append(APInterface(
                phy=phy,
                interface=iface_name,
                mac=iface_data.get('addr'),
                ssid=iface_data.get('ssid'),
                channel=channel,
                frequency=frequency,
                width=width
            ))
    
    return interfaces


# ============================================================================
# SSH Helper
# ============================================================================

class SSHConnection:
    """Simple SSH connection wrapper"""

    def __init__(self, hostname: str, username: str = 'root'):
        self.hostname = hostname
        self.username = username
        self.client = None
        self.last_rc = None
        self.last_error = None

    def __enter__(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            self.client.connect(
                self.hostname,
                username=self.username,
                look_for_keys=True,
                allow_agent=True,
                # das apk update kann laang dauern
                timeout=100
            )
        except Exception as e:
            raise ConnectionError(f"Failed to connect to {self.hostname}: {e}")
        
        return self

    def __exit__(self, *args):
        if self.client:
            self.client.close()

    def run(self, command: str) -> str:
        """Execute command and return output"""
        stdin, stdout, stderr = self.client.exec_command(command)
        self.last_rc = stdout.channel.recv_exit_status()
        output = stdout.read().decode('utf-8')
        self.last_error = stderr.read().decode('utf-8')
        
        if self.last_error and 'warning' not in self.last_error.lower():
            raise Exception(f"Command {command} failed: {self.last_error}")
        
        return output


# ============================================================================
# Neighbor Report Generation
# ============================================================================
def generate_neighbor_report(mac: str, channel: int) -> str:
    mac_clean = mac.replace(':', '').lower()

    # Operating Class (20 MHz, EU)
    if channel <= 13:
        op_class = 0x51
    elif 36 <= channel <= 48:
        op_class = 0x73
    elif 52 <= channel <= 64:
        op_class = 0x76
    elif 100 <= channel <= 144:
        op_class = 0x79
    elif 149 <= channel <= 161:
        op_class = 0x7c
    elif channel == 165:
        op_class = 0x7d
    else:
        raise ValueError(f"Unbekannter Kanal: {channel}")

    # BSSID Info (32 Bit, little-endian)
    info = 0x3          # Bit 0-1: erreichbar
    info |= 1 << 2      # Security: gleiche
    info |= 1 << 3      # Key Scope: gleicher Authenticator
    info |= 1 << 7      # Radio Measurement
    info |= 1 << 10     # Mobility Domain (FT)
    info |= 1 << 11     # HT
    if channel > 14:
        info |= 1 << 12 # VHT nur auf 5 GHz

    phy_type = 0x07 if channel <= 14 else 0x09  # HT bzw. VHT

    return (
        mac_clean
        + info.to_bytes(4, "little").hex()
        + f"{op_class:02x}{channel:02x}{phy_type:02x}"
        + "0301ff"   # Transition Candidate Preference
    )
