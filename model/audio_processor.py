import librosa
import soundfile as sf
import numpy as np

class AudioProcessor:
    def __init__(self, silence_threshold_db=30):
        self.silence_threshold_db = silence_threshold_db

    def trim_silences(self, input_path, output_path, keep_after=3.0, keep_before=1.0):
        """
        Trims silences longer than keep_after + keep_before.
        Keeps 'keep_after' seconds of silence after speech and 'keep_before' seconds before speech.
        """
        print(f"Loading audio for trimming: {input_path}")
        y, sr = librosa.load(input_path, sr=None)
        
        # Get non-silent intervals
        non_silent_intervals = librosa.effects.split(y, top_db=self.silence_threshold_db)
        
        if len(non_silent_intervals) == 0:
            # Entirely silent or below threshold? Return original
            return False, len(y) / sr, len(y) / sr
            
        original_duration = len(y) / sr
        processed_segments = []
        
        # Initial padding for the very first segment
        start_pad = max(0, non_silent_intervals[0][0] - int(keep_before * sr))
        end_pad = min(len(y), non_silent_intervals[0][1] + int(keep_after * sr))
        
        curr_start = start_pad
        curr_end = end_pad
        
        for i in range(1, len(non_silent_intervals)):
            next_start_raw = non_silent_intervals[i][0]
            next_end_raw = non_silent_intervals[i][1]
            
            next_start_padded = max(0, next_start_raw - int(keep_before * sr))
            next_end_padded = min(len(y), next_end_raw + int(keep_after * sr))
            
            # Max gap allowed is keep_after + keep_before (but we already padded them)
            # If they overlap or touch, merge
            if next_start_padded <= curr_end:
                curr_end = next_end_padded
            else:
                # Append current section
                processed_segments.append(y[curr_start:curr_end])
                curr_start = next_start_padded
                curr_end = next_end_padded
        
        # Append the last section
        processed_segments.append(y[curr_start:curr_end])
        
        trimmed_y = np.concatenate(processed_segments)
        
        sf.write(output_path, trimmed_y, sr)

        new_duration = len(trimmed_y) / sr
        trimmed_amount = original_duration - new_duration
        
        print(f"Trimming complete. Original: {original_duration:.2f}s, New: {new_duration:.2f}s, Trimmed: {trimmed_amount:.2f}s")
        return True, original_duration, new_duration

if __name__ == "__main__":
    # Test
    processor = AudioProcessor()
    # processor.trim_silences("test.wav", "test_trimmed.wav")
