"""
helpers.py - Helper functions and classes for OpenWrt roaming configuration
"""

import paramiko
import re
import json
from typing import Dict, List, Any, Tuple
from dataclasses import dataclass


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


@dataclass
class APInfo:
    """Complete information about an access point"""
    hostname: str
    interface_24: APInterface = None
    interface_5: APInterface = None


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
    """
    Generate 802.11k neighbor report string

    Format: BSSID (12 hex) + BSSID Info (8 hex) + Op Class (2 hex) + Channel (2 hex) + PHY Type (2 hex) + Sub-elements

    Operating Class (for Europe/AT):
      - 51 (0x33 hex, decimal 81) = 2.4 GHz channels 1-13
      - 73 (0x49 hex, decimal 115) = 5 GHz channels 36-64
      - 7c (0x7c hex, decimal 124) = 5 GHz channels 149-165
    """
    mac_clean = mac.replace(':', '').lower()
    channel_hex = f"{channel:02x}"

    # Operating Class
    if channel <= 14:
        op_class = "51"  # 2.4 GHz (decimal 81 = 0x51 hex!)
    elif channel >= 36 and channel <= 64:
        op_class = "73"  # 5 GHz lower (5.15-5.35 GHz)
    elif channel >= 100 and channel <= 144:
        op_class = "73"  # 5 GHz middle (5.47-5.725 GHz)
    elif channel >= 149:
        op_class = "7c"  # 5 GHz upper (5.725-5.875 GHz)
    else:
        op_class = "51"  # Fallback

   # BSSID Info: 
    # Bit 0: AP Reachability (1 = reachable)
    # Bit 1: Security (1 = same security)
    # Bit 2: Key Scope (1 = AP has same authenticator)
    # Bit 3-9: Capabilities (HT, VHT, FT, etc.)
    # BSSID Info: ff19 = reachable, same security, same authenticator, HT/VHT capable
    bssid_info = "ff190000"

    # PHY Type: 07 = HT (802.11n), 09 = VHT (802.11ac)
    phy_type = "07"

    # Sub-elements: 
    # 03 = BSS Transition Candidate Preference
    # 01 = length
    # 00 = preference value (0-255, higher = better)
    sub_elements = "030100"
    
    return f"{mac_clean}{bssid_info}{op_class}{channel_hex}{phy_type}{sub_elements}"

