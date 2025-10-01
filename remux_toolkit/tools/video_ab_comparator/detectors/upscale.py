# remux_toolkit/tools/video_ab_comparator/detectors/upscale.py

from .base_detector import BaseDetector
from ..core.source import VideoSource
import cv2
import numpy as np
from typing import List

class UpscaleDetector(BaseDetector):
    @property
    def issue_name(self) -> str:
        return "Upscaled Video"

    def run(self, source: VideoSource, frame_list: List[np.ndarray]) -> dict:
        if not source.info or not source.info.streams:
            return {'score': -1, 'summary': 'No video stream found'}

        video_stream = next((s for s in source.info.streams if s.codec_type == 'video'), None)
        if not video_stream or not video_stream.resolution:
            return {'score': -1, 'summary': 'Could not determine resolution'}

        width, height = map(int, video_stream.resolution.split('x'))

        if height < 720:
             return {'score': 0, 'summary': f'Native SD ({height}p)'}

        if height >= 1080:
            try:
                if not frame_list:
                    return {'score': -1, 'summary': 'Failed to extract frame'}

                frame = frame_list[len(frame_list) // 2] # Use middle frame

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()

                if lap_var < 100:
                    return {'score': 90, 'summary': f'Likely Upscaled (Laplacian Variance: {lap_var:.2f})'}
                else:
                    return {'score': 10, 'summary': f'Likely Native HD (Laplacian Variance: {lap_var:.2f})'}

            except Exception as e:
                return {'score': -1, 'summary': f'Error during analysis: {e}'}

        return {'score': 0, 'summary': 'Standard Definition'}
