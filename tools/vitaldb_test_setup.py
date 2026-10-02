# VitalDB Test & Pretraining Infrastructure
# Public intraoperative database for testing our pipeline methods
# API: https://api.vitaldb.net/ (no credentialing required)
# Python: pip install vitaldb
#
# Why VitalDB:
# - 6,388 surgical cases, same data type as K's (intraoperative)
# - BIS + EEG, propofol/remifentanil infusions, arterial pressures
# - Use for: method testing, SSL pretraining, benchmarking
# - K's data stays private; VitalDB is public so we can iterate freely

import os
import sys

# Test plan:
# 1. Download N cases via VitalDB API (start with 10 for smoke test)
# 2. Run each filter method from master_dictionary.yaml
# 3. Compare: does wavelet beat bandpass? Does Kalman beat Hampel?
# 4. SSL pretraining: masked reconstruction on VitalDB waveforms
# 5. Benchmark harness validates all results

VITALDB_API = "https://api.vitaldb.net"

# Key endpoints:
# - /cases : clinical information (demographics, surgery type, outcomes)
# - /trks   : track list (what signals each case has)
# - /{tid}  : track data (actual waveforms/parameters)
# - /labs   : laboratory results

# Relevant tracks for our pipeline:
RELEVANT_TRACKS = [
    # BIS (like K's BIS data)
    "BIS/BIS",           # BIS index
    "BIS/SQI",           # Signal quality
    "BIS/EMG",           # EMG
    "BIS/EEG1_WAV",      # EEG channel 1 (128 Hz)
    "BIS/EEG2_WAV",      # EEG channel 2 (128 Hz)
    
    # Cardiovascular (like K's Infinity/BetterCare)
    "SNUADC/ECG_II",     # ECG lead II (500 Hz)
    "SNUADC/PLETH",      # Plethysmograph (500 Hz)
    "SNUADC/ART",        # Arterial pressure waveform (500 Hz)
    
    # Drugs (like K's pump data)
    # Propofol/remifentanil infusion rates via Orchestra pump tracks
    
    # Ventilator (like K's Infinity)
    # Primus ventilator tracks
]

def download_test_cases(n_cases=10, output_dir="vitaldb_test"):
    """
    Download N VitalDB cases for pipeline testing.
    Start small (10 cases) for smoke test, scale up for pretraining.
    """
    import vitaldb
    
    os.makedirs(output_dir, exist_ok=True)
    
    # Find cases with BIS + ECG (most relevant to K's data)
    track_names = ['BIS/BIS', 'SNUADC/ECG_II']
    case_ids = vitaldb.find_cases(track_names)
    
    print(f"Found {len(case_ids)} cases with BIS + ECG")
    
    selected = case_ids[:n_cases]
    for case_id in selected:
        print(f"Downloading case {case_id}...")
        # Download BIS data (1 Hz) and ECG (500 Hz)
        bis = vitaldb.load_case(case_id, ['BIS/BIS'], 1)
        ecg = vitaldb.load_case(case_id, ['SNUADC/ECG_II'], 1/500)
        
        # Save for testing
        bis.to_csv(f"{output_dir}/case_{case_id}_bis.csv")
        # ECG is large, save as parquet
        ecg.to_parquet(f"{output_dir}/case_{case_id}_ecg.parquet")
    
    print(f"Downloaded {len(selected)} test cases to {output_dir}/")
    return selected

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    download_test_cases(n)
