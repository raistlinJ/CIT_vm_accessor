#!/bin/bash

# Ensure the script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root."
  exit 1
fi

echo "Scanning Proxmox SPICE VMs to reset clipboard and set video memory to 128MB..."
echo "--------------------------------------------------------"

# Loop through all VM IDs on the node
for vmid in $(qm list | awk 'NR>1 {print $1}'); do
    # Get the raw vga configuration line from qm config
    vga_config=$(qm config "$vmid" | grep "^vga:")

    if [ -n "$vga_config" ]; then
        # Isolate the exact value assigned to vga (remove the 'vga: ' prefix)
        raw_value=$(echo "$vga_config" | sed 's/^vga:[[:space:]]*//')
        
        # Check if the display type contains any form of SPICE (qxl, virtio, etc.)
        if [[ "$raw_value" == *"qxl"* || "$raw_value" == *"virtio"* ]]; then
            
            echo "VM $vmid: Updating SPICE clipboard and video memory."
            echo "  Current config line: vga: $raw_value"
            
            # 1. Surgically strip 'clipboard=vnc'
            cleaned_value=$(echo "$raw_value" | sed -E \
                -e 's/,clipboard=vnc//g' \
                -e 's/clipboard=vnc,//g' \
                -e 's/clipboard=vnc//g')
            
            # 2. Update or append the memory=128 setting
            if [[ "$cleaned_value" == *"memory="* ]]; then
                # Replace existing memory configuration with 128
                new_value=$(echo "$cleaned_value" | sed -E 's/(^|,)memory=[0-9]+/\1memory=128/g')
            else
                # If memory isn't specified on the line, append it
                new_value="${cleaned_value},memory=128"
            fi
            
            echo "  Target config line:  vga: $new_value"

            if [[ "$raw_value" == "$new_value" ]]; then
                echo "VM $vmid: Already uses default clipboard and video memory=128."
                echo "--------------------------------------------------------"
                continue
            fi
            
            # Apply the final updated string back to the VM configuration
            qm set "$vmid" --vga "$new_value"
            
            if [ $? -eq 0 ]; then
                echo "VM $vmid: Config updated successfully."
                echo "Reminder: Completely STOP (Power Off) and START the VM to apply changes."
            else
                echo "VM $vmid: Error updating configuration."
            fi
            echo "--------------------------------------------------------"
        fi
    fi
done

echo "Process complete."
