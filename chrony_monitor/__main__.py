#!/usr/bin/env python3
"""Command-line entry point for chrony monitor."""

import argparse
import sys

from . import __version__
from .monitor import MonitorConfig, run_monitor
from .status import get_status, has_usb_gps, has_pps_device


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        prog="chrony-monitor",
        description="Monitor chrony time synchronization with GPS PPS and NTP support.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    Auto-detect mode (GPS PPS or NTP)
  %(prog)s --ntp-only         Force NTP-only mode
  %(prog)s --no-recovery      Disable auto-recovery for PPS
  %(prog)s --interval 2       Poll every 2 seconds

Colors:
  Green   - Excellent sync (GPPS locked or NTP <1ms offset)
  Blue    - Good NTP sync (<50ms offset)
  Yellow  - Degraded, recovering, or PPS issue
  Red     - No sync or daemon error
"""
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}"
    )

    parser.add_argument(
        "--ntp-only",
        action="store_true",
        help="Force NTP-only mode (ignore GPS/PPS hardware)"
    )

    parser.add_argument(
        "--no-recovery",
        action="store_true",
        help="Disable automatic PPS recovery attempts"
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Polling interval in seconds (default: 1.0)"
    )

    parser.add_argument(
        "--recovery-timeout",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds to wait before attempting recovery (default: 60)"
    )

    parser.add_argument(
        "--recovery-cooldown",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Seconds between recovery attempts (default: 300)"
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current status and exit (no monitor UI)"
    )

    return parser


def print_status():
    """Print current status to stdout and exit."""
    status = get_status()

    print(f"Chrony Monitor Status")
    print("=" * 40)

    # Hardware detection
    usb_gps = has_usb_gps()
    pps_device = has_pps_device()
    print(f"USB GPS detected:  {'Yes' if usb_gps else 'No'}")
    print(f"PPS device exists: {'Yes' if pps_device else 'No'}")
    print(f"Expected mode:     {'GPS PPS' if status.pps_expected else 'NTP only'}")
    print()

    # Sync status
    print(f"Sync state:   {status.sync_state.value}")
    print(f"Sync quality: {status.sync_quality.value}")
    if status.offset_ms is not None:
        print(f"Offset:       {status.offset_ms:.3f} ms")
    print()

    # Selected source
    if status.selected_source:
        src = status.selected_source
        print(f"Selected source: {src.name}")
        print(f"  Stratum: {src.stratum}")
        print(f"  Reach:   {src.reach}")
        print(f"  LastRx:  {src.last_rx}")
        print(f"  Type:    {'PPS' if src.is_pps else 'GPS' if src.is_gps else 'NTP'}")
    else:
        print("No source selected")

    if status.error_message:
        print(f"\nError: {status.error_message}")

    # All sources
    if status.sources:
        print(f"\nAll sources ({len(status.sources)}):")
        for src in status.sources:
            marker = "*" if src.is_selected else " "
            print(f"  {marker} {src.name:20} St:{src.stratum} Reach:{src.reach}")


def main():
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0

    config = MonitorConfig(
        interval=args.interval,
        ntp_only=args.ntp_only,
        recovery_enabled=not args.no_recovery,
        recovery_timeout=args.recovery_timeout,
        recovery_cooldown=args.recovery_cooldown
    )

    try:
        run_monitor(config)
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
