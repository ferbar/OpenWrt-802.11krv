#!/usr/bin/env python3.11
# https://claude.ai/chat/1b2be424-79aa-4844-8e58-d62c609af64c
# sudo zypper install python3-colorama python3-paramiko
# sudo zypper install python311-paramiko python311-colorama
# --apply => auch wenn dry_run=1 applien
#!/usr/bin/env python3
"""
OpenWrt 802.11r/k/v Auto-Configuration Script

Requirements:
  pip install paramiko colorama

Files needed:
  - main.py (this file)
  - helpers.py
  - roaming-config.json

Usage:
  1. Create/edit roaming-config.json
  2. Set "dry_run": true for testing
  3. Run: python3 main.py
  4. Set "dry_run": false and run again to apply
"""

import json
import sys
import os
import secrets
import time
import argparse

from typing import Dict, List, Any, Tuple
from colorama import Fore, Style, init
from helpers import APInfo, SSHConnection, parse_iw_dev, generate_neighbor_report
from pprint import pprint

init(autoreset=True)

# ============================================================================
# Configuration Loading
# ============================================================================

def load_config(config_file: str = 'roaming-config.json') -> Dict:
    """Load configuration from JSON file"""
    if not os.path.exists(config_file):
        print(f"{Fore.RED}Error: Config file '{config_file}' not found!{Style.RESET_ALL}")
        print(f"\nCreate a config file with this content:")
        print(json.dumps({
            "ssid": "your-ssid",
            "mobility_domain": "beef",
            "ssh_user": "root",
            "dry_run": True,
            "ap_hosts": ["ap1", "ap2", "ap3"]
        }, indent=2))
        sys.exit(1)
    
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        required = ['ssid', 'mobility_domain', 'ap_hosts']
        missing = [f for f in required if f not in config]
        
        if missing:
            print(f"{Fore.RED}Error: Missing required config fields: {', '.join(missing)}{Style.RESET_ALL}")
            sys.exit(1)
        
        config.setdefault('ssh_user', 'root')
        config.setdefault('dry_run', True)
        
        return config
    except json.JSONDecodeError as e:
        print(f"{Fore.RED}Error parsing config: {e}{Style.RESET_ALL}")
        sys.exit(1)


def collect_ap_info(hostname: str, ssid: str) -> APInfo:
    """Connect to AP and collect interface information"""
    print(f"  Connecting to {Fore.CYAN}{hostname}{Style.RESET_ALL}...", end=" ", flush=True)
    
    try:
        with SSHConnection(hostname) as ssh:
            output = ssh.run('iw dev')
            interfaces = parse_iw_dev(output)

            # Filter by SSID and separate 2.4/5 GHz
            ap_info = APInfo(hostname=hostname)
            
            for iface in interfaces:
                if iface.ssid == ssid:
                    if iface.is_24ghz():
                        ap_info.interface_24 = iface
                    elif iface.is_5ghz():
                        ap_info.interface_5 = iface
            
            if not ap_info.interface_24 and not ap_info.interface_5:
                print(f"{Fore.RED}ERROR: No matching SSID found!{Style.RESET_ALL}")
                pprint(interfaces)
                return None
            
            print(f"{Fore.GREEN}OK{Style.RESET_ALL}")
            if ap_info.interface_24:
                print(f"    2.4G: {ap_info.interface_24.mac} ch {ap_info.interface_24.channel}")
            if ap_info.interface_5:
                print(f"    5G: {ap_info.interface_5.mac} ch {ap_info.interface_5.channel}")
            
            return ap_info
    
    except ConnectionError as e:
        print(f"{Fore.RED}CONNECTION FAILED{Style.RESET_ALL}")
        print(f"    {e}")
        raise
    except Exception as e:
        print(f"{Fore.RED}ERROR: {e}{Style.RESET_ALL}")
        raise


def ensure_package_installed(hostname: str, package: str, dry_run: bool = False, remove: list[str] = None) -> bool:
    """Ensure static-neighbor-reports package is installed"""

    print(f"  Checking {package} package...", end=" ", flush=True)

    
    try:
        with SSHConnection(hostname) as ssh:
            ssh.run("which opkg")
            has_opkg = not ssh.last_rc
            ssh.run("which apk")
            has_apk = not ssh.last_rc
            if(not has_apk and not has_opkg):
                raise Exception("didn't find apk or opkg")
            if(has_opkg):
                cmd_is_installed=f"opkg list-installed | grep {package} || echo ''"
                cmd_install=f"opkg update > /dev/null 2>&1 && opkg install {package}"
            else:
                cmd_is_installed=f"apk info 2>/dev/null | grep {package} || echo ''"
                # es können nicht alle pkg quellen runtergeladen werden, daher is uns der RC einmal wurscht...
                cmd_install=f"apk update ; apk add {package}"


            installed = ssh.run(cmd_is_installed)
            if not installed.strip():
                print(f"{Fore.YELLOW}NOT INSTALLED{Style.RESET_ALL}")
                if remove:
                    print(f"!!!!!!!!!!!!!!! please remove {remove} manually!!!!!!!!!!!!!!!!!!!!!!!!!!")
                
                if dry_run:
                    print(f"    {Fore.YELLOW}[DRY RUN] Would install package{Style.RESET_ALL}")
                    print(f"    {Fore.GREEN}          {cmd_install}{Style.RESET_ALL}")
                    return True
                else:
                    print(f"    Installing...", end=" ", flush=True)
                    ssh.run(cmd_install)
                    if(ssh.last_rc != 0):
                        raise Exception(f"Error installing package {package}! {cmd_install} -> {ssh.last_error}")
            else:
                print(f"{Fore.GREEN}OK{Style.RESET_ALL}")
            return True
    except Exception as e:
        print(f"{Fore.RED}ERROR: {e}{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}Please install manually: opkg install {package}{Style.RESET_ALL}")
        sys.exit(1)


def configure_neighbors(ap: APInfo, all_aps: List[APInfo], ssid: str, dry_run: bool = False):
    """Configure neighbor reports using static-neighbor-report package (cross-band!)"""
    print(f"\n{Fore.GREEN}Configuring neighbors on {ap.hostname}:{Style.RESET_ALL}")

    # Check/install package
    #openwrt 25.12: static-neighbor-reports mit 's'
    if not ensure_package_installed(ap.hostname, "static-neighbor-reports", dry_run):
        return

    if not ensure_package_installed(ap.hostname, "wpad-mbedtls", dry_run, ['wpad-basic', 'wpad-basic-mbedtls'] ):
        return

    # Delete old neighbor entries
    commands = ["while uci -q delete static-neighbor-report.@neighbor[0]; do :; done"]
    neighbor_count = 0

    # Determine which interfaces this AP has
    interfaces_on_this_ap = []
    if ap.interface_24:
        interfaces_on_this_ap.append(ap.interface_24)
    if ap.interface_5:
        interfaces_on_this_ap.append(ap.interface_5)

    # For each interface on this AP, add ALL other APs as neighbors (cross-band!)
    for my_iface in interfaces_on_this_ap:
        iface_name = my_iface.interface
        band_str = "2.4G" if my_iface.is_24ghz() else "5G"
        
        print(f"  Configuring {band_str} interface ({iface_name}):")

        commands.extend([
            f"uci set wireless.default_radio{my_iface.phy}.ieee80211k='1'",
            f"uci set wireless.default_radio{my_iface.phy}.rrm_neighbor_report='1'",
            f"uci set wireless.default_radio{my_iface.phy}.rrm_beacon_report='1'" ])

        # Add all other APs (both bands) as neighbors
        for other_ap in all_aps:
            if other_ap.hostname == ap.hostname:
                continue  # Skip ourselves

            # Add 2.4 GHz neighbor if exists
            if other_ap.interface_24:
                nr = generate_neighbor_report(other_ap.interface_24.mac, other_ap.interface_24.channel)
                print(f"    + {other_ap.hostname} 2.4G ({other_ap.interface_24.mac}, ch {other_ap.interface_24.channel})")
                
                commands.extend([
                    "uci add static-neighbor-report neighbor",
                    f"uci set static-neighbor-report.@neighbor[-1].bssid='{other_ap.interface_24.mac}'",
                    f"uci set static-neighbor-report.@neighbor[-1].ssid='{ssid}'",
                    f"uci set static-neighbor-report.@neighbor[-1].neighbor_report='{nr}'",
                    f"uci set static-neighbor-report.@neighbor[-1].iface='{iface_name}'",
                    f"uci set static-neighbor-report.@neighbor[-1].disabled='0'"
                ])
                neighbor_count += 1

            # Add 5 GHz neighbor if exists
            if other_ap.interface_5:
                nr = generate_neighbor_report(other_ap.interface_5.mac, other_ap.interface_5.channel)
                print(f"    + {other_ap.hostname} 5G ({other_ap.interface_5.mac}, ch {other_ap.interface_5.channel})")
                
                commands.extend([
                    "uci add static-neighbor-report neighbor",
                    f"uci set static-neighbor-report.@neighbor[-1].bssid='{other_ap.interface_5.mac}'",
                    f"uci set static-neighbor-report.@neighbor[-1].ssid='{ssid}'",
                    f"uci set static-neighbor-report.@neighbor[-1].neighbor_report='{nr}'",
                    f"uci set static-neighbor-report.@neighbor[-1].iface='{iface_name}'",
                    f"uci set static-neighbor-report.@neighbor[-1].disabled='0'"
                ])
                neighbor_count += 1
    
    if neighbor_count == 0:
        print(f"  {Fore.YELLOW}No neighbors to configure{Style.RESET_ALL}")
        return

    # Commit and restart service
    commands.extend([
        "uci commit static-neighbor-report",
        "/etc/init.d/static-neighbor-reports enable",
        "/etc/init.d/static-neighbor-reports restart"
    ])
    
    if dry_run:
        print(f"\n  {Fore.YELLOW}[DRY RUN] Would execute:{Style.RESET_ALL}")
        print(f"    {commands[0]}")  # Delete old
        print(f"    ... {neighbor_count} neighbor blocks ...")
        for cmd in commands[-3:]:  # Last 3 commands
            print(f"    {cmd}")
    else:
        try:
            with SSHConnection(ap.hostname) as ssh:
                ssh.run(" && ".join(commands))
            print(f"  {Fore.GREEN}✓ {neighbor_count} neighbors configured and service restarted{Style.RESET_ALL}")
        except Exception as e:
            print(f"  {Fore.RED}✗ Error: {e}{Style.RESET_ALL}")


def configure_r0kh_r1kh(ap: APInfo, all_aps: List[APInfo],
                        mobility_domain: str, shared_secret: str, dry_run: bool = False):
    """Configure R0KH/R1KH lists for PMK R1 Push"""
    print(f"\n{Fore.GREEN}Configuring R0KH/R1KH on {ap.hostname}:{Style.RESET_ALL}")
    
    commands = []
    
    # Delete old R0KH/R1KH entries
    radios_to_setup = []
    if ap.interface_24:
        radios_to_setup.append(ap.interface_24)
    if ap.interface_5:
        radios_to_setup.append(ap.interface_5)
    
    for radio in radios_to_setup:
        if radio.is_24ghz:
            print(f"  Configuring 2.4 GHz ({radio.frequency} phy:{radio.phy})")
        if radio.is_5ghz:
            print(f"  Configuring 5 GHz ({radio.frequency} phy:{radio.phy})")

        radio_name = f"default_radio{radio.phy}"
        r1_key_holder = radio.mac.replace(":", "")
        print(f"  Setting Fast Transition Config {radio_name}...")
        # FT geht mit generate_local=1 und wpa2; wpa2+3 braucht generate_local=0 und die R0KH und R1KH richtig ausgefüllt
        #commands.append(f"uci set wireless.{radio_name}.encryption='sae-mixed'")
        commands.append(f"uci set wireless.{radio_name}.encryption='psk2+ccmp'")
        # 802.11w Management Frame Protection 1 == optional
        commands.append(f"uci set wireless.{radio_name}.ieee80211w='1'")
        # WMM default option =1 => müss ma ned extra setzen
        # 802.11v unterstützung aktiviern, das alleine tut noch nix
        commands.append(f"uci set wireless.{radio_name}.bss_transition='1'")
        commands.append(f"uci set wireless.{radio_name}.mobility_domain='{mobility_domain}'")
        commands.append(f"uci set wireless.{radio_name}.ft_over_ds='1'")
        # mit ft_psk_generate_local=0 bleibt das handy auf einem AP picken. angeblich geht bei '1' auch fast transition mit wpa2/3 mixed
        ft_psk_generate_local=0
        commands.append(f"uci set wireless.{radio_name}.ft_psk_generate_local='{ft_psk_generate_local}'")
        # nasid = r0kh-id = r1_key_holder
        commands.append(f"uci set wireless.{radio_name}.nasid='{r1_key_holder}'")
        commands.append(f"uci set wireless.{radio_name}.r1_key_holder='{r1_key_holder}'")
        # bei rssi=-72 kicken, disassoc_low_ack=1 =default
        commands.append(f"uci set wireless.{radio_name}.rssi_reject_assoc_rssi='-75'")
        
        print(f"  Cleaning old r0kh/r1kh {radio_name}...")
        commands.append(f"uci -q delete wireless.{radio_name}.r0kh || true")
        commands.append(f"uci -q delete wireless.{radio_name}.r1kh || true")

        # brauch ma nur fast transition (802.11r) wenn ft_psk_generate_local == 0
        if ft_psk_generate_local == 0:
            for other_ap in all_aps:
                print(f"   adding r0kh/r1kh from {other_ap.hostname}")
    #            den eigenen host in die liste eintragen soll gescheiter sein, hostapd erkennt angeblich eh wenns die eigene mac ist
    #            if other_ap.hostname == ap.hostname:
    #                print(f"    skipping, it's me")
    #                continue
                if other_ap.interface_24:
                    mac = other_ap.interface_24.mac
                    r1_key_holder = mac.replace(":", "")
                    # R0KH: MAC, nasid == r0kh-id, Secret
                    r0kh = f"{mac},{r1_key_holder},{shared_secret}"
                    # R1KH: MAC, R1KH-ID (= MAC with colons!), Secret
                    r1kh = f"{mac},{mac},{shared_secret}"
                    commands.append(f"uci add_list wireless.{radio_name}.r0kh='{r0kh}'")
                    commands.append(f"uci add_list wireless.{radio_name}.r1kh='{r1kh}'")
    
                if other_ap.interface_5:
                    mac = other_ap.interface_5.mac
                    r1_key_holder = mac.replace(":", "")
                    r0kh = f"{mac},{r1_key_holder},{shared_secret}"
                    r1kh = f"{mac},{mac},{shared_secret}"
                    commands.append(f"uci add_list wireless.{radio_name}.r0kh='{r0kh}'")
                    commands.append(f"uci add_list wireless.{radio_name}.r1kh='{r1kh}'")
    
    if len(commands) <= len(radios_to_setup) * 2:
        print(f"  {Fore.YELLOW}No radios to configure{Style.RESET_ALL}")
        return
    
    commands.extend(["uci commit wireless", "wifi reload"])
    
    if dry_run:
        print(f"\n  {Fore.YELLOW}[DRY RUN] Would execute:{Style.RESET_ALL}")
        for cmd in commands[:4]:  # First 4 (cleaning)
            print(f"    {cmd}")
        if len(commands) > 8:
            print(f"    ... ({len(commands) - 8} more r0kh/r1kh commands) ...")
        for cmd in commands[-2:]:  # Last 2
            print(f"    {cmd}")
    else:
        try:
            with SSHConnection(ap.hostname) as ssh:
                ssh.run(" && ".join(commands))
            print(f"  {Fore.GREEN}✓ R0KH/R1KH configured{Style.RESET_ALL}")
        except Exception as e:
            print(f"  {Fore.RED}✗ Error: {e}{Style.RESET_ALL}")


def verify_configuration(ap_info: APInfo):
    """Verify the configuration on an AP"""
    print(f"\n{Fore.GREEN}{ap_info.hostname}:{Style.RESET_ALL}")
    
    try:
        with SSHConnection(ap_info.hostname) as ssh:
            # Check static-neighbor-report config
            neighbors = ssh.run("uci show static-neighbor-report 2>/dev/null | grep '.bssid=' || true")
            if neighbors.strip():
                count = len(neighbors.strip().split('\n'))
                print(f"  Static neighbors: {count} configured")
            else:
                print(f"  {Fore.YELLOW}No static neighbors configured{Style.RESET_ALL}")
            
            # Check R0KH/R1KH
            r0kh = ssh.run("uci show wireless | grep 'r0kh=' || true")
            r1kh = ssh.run("uci show wireless | grep 'r1kh=' || true")
            
            if r0kh.strip() and r1kh.strip():
                r0_count = len([l for l in r0kh.strip().split('\n') if 'r0kh=' in l])
                r1_count = len([l for l in r1kh.strip().split('\n') if 'r1kh=' in l])
                print(f"  R0KH/R1KH: {r0_count}/{r1_count} entries")
            
            # Check runtime via ubus
            print(f"  {Fore.CYAN}Runtime status:{Style.RESET_ALL}")
            
            hostapd_ifaces = ssh.run("ubus list | grep 'hostapd\\.phy' || true")
            for iface_line in hostapd_ifaces.strip().split('\n'):
                iface = iface_line.strip()
                if not iface:
                    continue
                try:
                    nr_output = ssh.run(f"ubus call {iface} rrm_nr_list 2>/dev/null || echo '{{}}'")
                    nr_data = json.loads(nr_output)
                    
                    if 'list' in nr_data:
                        count = len(nr_data.get('list', []))
                        if count > 0:
                            print(f"    {iface}: {Fore.GREEN}{count} neighbors active ✓{Style.RESET_ALL}")
                        else:
                            print(f"    {iface}: {Fore.YELLOW}0 neighbors (waiting for service restart?){Style.RESET_ALL}")
                except Exception as e:
                    print(f"    {iface}: (" + str(e) + ")")
    except Exception as e:
        print(f"  {Fore.RED}Error: {e}{Style.RESET_ALL}")


# ============================================================================
# Main
# ============================================================================

def main():
    print(f"{Fore.GREEN}{'='*70}")
    print("OpenWrt 802.11r/k/v Auto-Configuration Script")
    print(f"{'='*70}{Style.RESET_ALL}\n")
    
    config = load_config()
    ssid = config['ssid']
    mobility_domain = config['mobility_domain']
    ssh_user = config['ssh_user']
    dry_run = config['dry_run']
    ap_hosts = config['ap_hosts']
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        dry_run = False
    
    print(f"Configuration:")
    print(f"  SSID: {ssid}")
    print(f"  Mobility Domain: {mobility_domain}")
    print(f"  APs: {', '.join(ap_hosts)}")
    print(f"  Mode: {Fore.YELLOW}DRY RUN{Style.RESET_ALL}" if dry_run else f"  Mode: {Fore.RED}LIVE (will apply changes){Style.RESET_ALL}")
    print()
    
    if dry_run:
        print(f"{Fore.YELLOW}{'='*70}")
        print("DRY RUN MODE - No changes will be applied")
        print(f"{'='*70}{Style.RESET_ALL}\n")
    
    # Step 1: Collect AP information
    print(f"{Fore.YELLOW}Step 1: Collecting AP information...{Style.RESET_ALL}\n")
    
    all_aps = []
    failed_aps = []
    for hostname in ap_hosts:
        try:
            ap_info = collect_ap_info(hostname, ssid)
            if ap_info:
                all_aps.append(ap_info)
            else:
                failed_aps.append(hostname)
            print()
        except Exception as e:
            print(f"\n{Fore.RED}FATAL: Could not connect to {hostname}{Style.RESET_ALL}")
            print(f"Error: {e}\n")
            print(f"{Fore.RED}Aborting - all APs must be reachable!{Style.RESET_ALL}")
            sys.exit(1)
    
    if not all_aps:
        print(f"\n{Fore.RED}No APs found! Check hostnames and SSH connectivity.{Style.RESET_ALL}")
        sys.exit(1)
    
    if failed_aps:
        print(f"\n{Fore.RED}Some APs did not have matching SSID: {', '.join(failed_aps)}{Style.RESET_ALL}")
        print(f"{Fore.RED}Aborting!{Style.RESET_ALL}")
        sys.exit(1)
    
    print(f"{Fore.GREEN}✓ All {len(all_aps)} APs reachable and configured{Style.RESET_ALL}")
    
    # Step 2: Generate shared secret
    print(f"\n{Fore.YELLOW}Step 2: Generating shared secret for R0KH/R1KH...{Style.RESET_ALL}\n")
    shared_secret = secrets.token_hex(32)
    print(f"  Secret (64 hex chars): {shared_secret[:24]}...{shared_secret[-16:]}")
    
    # Step 3: Configure neighbor reports
    print(f"\n{Fore.YELLOW}Step 3: Configuring 802.11k neighbor reports (cross-band)...{Style.RESET_ALL}")
    for ap in all_aps:
        configure_neighbors(ap, all_aps, ssid, dry_run)
    
    # Step 4: Configure R0KH/R1KH
    print(f"\n{Fore.YELLOW}Step 4: Configuring R0KH/R1KH for 802.11r PMK R1 Push...{Style.RESET_ALL}")
    for ap in all_aps:
        configure_r0kh_r1kh(ap, all_aps, mobility_domain, shared_secret, dry_run)
    
    # Step 5: Verify
    if not dry_run:
        time.sleep(4)
        print(f"\n{Fore.YELLOW}Step 5: Verifying configuration...{Style.RESET_ALL}")
        for ap in all_aps:
            verify_configuration(ap)
    
    # Summary
    print(f"\n{Fore.GREEN}{'='*70}")
    if dry_run:
        print("DRY RUN Complete - Review output above")
        print(f"{'='*70}{Style.RESET_ALL}\n")
        print(f"{Fore.YELLOW}To apply changes:{Style.RESET_ALL}")
        print("  Run script again with --apply")
    else:
        print("Configuration Complete!")
        print(f"{'='*70}{Style.RESET_ALL}\n")
        
        print("Configured APs:")
        for ap in all_aps:
            print(f"  ✓ {ap.hostname}")
            if ap.interface_24:
                print(f"    2.4G: {ap.interface_24.mac} (ch {ap.interface_24.channel})")
            if ap.interface_5:
                print(f"    5G: {ap.interface_5.mac} (ch {ap.interface_5.channel})")
        
        print(f"\n{Fore.CYAN}Verify with these commands:{Style.RESET_ALL}")
        if all_aps[0].interface_24:
            iface = all_aps[0].interface_24.interface
            print(f"  # Check neighbor reports:")
            print(f"  ssh root@{all_aps[0].hostname} \"ubus call hostapd.{iface} rrm_nr_list\"")
        print(f"\n  # Monitor roaming events:")
        print(f"  ssh root@{all_aps[0].hostname} \"logread -f | grep -E 'PMK|R0KH|R1KH|FT'\"")
        print(f"\n  # Test from client:")
        print(f"  iw dev wlan0 scan | grep -A 30 '{ssid}'")
        print(f"  wpa_cli -i wlan0 neighbor_rep_request")
    
    print()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n{Fore.YELLOW}Interrupted by user{Style.RESET_ALL}")
        sys.exit(130)
    except Exception as e:
        print(f"\n{Fore.RED}Fatal error: {e}{Style.RESET_ALL}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
