# Copyright The Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os

import numpy as np

from typing import *
from functools import lru_cache
from torchmetrics.utilities import rank_zero_info
from torch import Tensor
import torch
import requests

from torchmetrics.utilities.checks import _check_same_shape
from torchmetrics.utilities.imports import _LIBROSA_AVAILABLE, _ONNXRUNTIME_AVAILABLE

if _LIBROSA_AVAILABLE and _ONNXRUNTIME_AVAILABLE:
    import librosa
    import onnxruntime as ort
else:
    librosa, ort = None, None

__doctest_requires__ = {("deep_noise_suppression_mean_opinion_score",): ["librosa", "onnxruntime"]}

SAMPLING_RATE = 16000
INPUT_LENGTH = 9.01
DNSMOS_DIR = "~/.torchmetrics/DNSMOS"


def _prepare_DNSMOS(dnsmos_dir: str):
    """download the DNSMOS files: "DNSMOS/DNSMOS/model_v8.onnx", "DNSMOS/DNSMOS/sig_bak_ovr.onnx", "DNSMOS/pDNSMOS/sig_bak_ovr.onnx"

    Args:
        dnsmos_dir: a dir to save the downloaded files. Defaults to "~/.torchmetrics".
    """
    # https://raw.githubusercontent.com/microsoft/DNS-Challenge/master/DNSMOS/DNSMOS/model_v8.onnx
    # https://raw.githubusercontent.com/microsoft/DNS-Challenge/master/DNSMOS/DNSMOS/sig_bak_ovr.onnx
    # https://raw.githubusercontent.com/microsoft/DNS-Challenge/master/DNSMOS/pDNSMOS/sig_bak_ovr.onnx
    url = "https://raw.githubusercontent.com/microsoft/DNS-Challenge/master"
    dnsmos_dir = os.path.expanduser(dnsmos_dir)

    # save to or load from ~/torchmetrics/dnsmos/.
    for file in ["DNSMOS/DNSMOS/model_v8.onnx", "DNSMOS/DNSMOS/sig_bak_ovr.onnx", "DNSMOS/pDNSMOS/sig_bak_ovr.onnx"]:
        saveto = os.path.join(dnsmos_dir, file.removeprefix("DNSMOS/"))
        os.makedirs(os.path.dirname(saveto), exist_ok=True)
        if os.path.exists(saveto):
            # try load onnx
            try:
                sess = ort.InferenceSession(saveto)
                continue  # skip downloading if succeeded
            except:
                ...
        urlf = f'{url}/{file}'
        rank_zero_info(f'downloading {urlf} to {saveto}')
        myfile = requests.get(urlf)
        open(saveto, 'wb').write(myfile.content)


@lru_cache
def _load_session(
    path: str,
    device: torch.device,
):
    """load onnxruntime session

    Args:
        path: the model path
        device: the device used

    Returns:
        onnxruntime session
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        _prepare_DNSMOS(DNSMOS_DIR)

    if device.type == 'cpu':
        return ort.InferenceSession(path)
    else:
        assert device.type == 'cuda', device
        providers = ['CUDAExecutionProvider']
        provider_options = [{"device_id": device.index}]
        return ort.InferenceSession(path, providers=providers, provider_options=provider_options)


def _audio_melspec(audio, n_mels=120, frame_size=320, hop_length=160, sr=16000, to_db=True):
    """calculate the mel-spectrogram of an audio

    Args:
        audio: [..., T]
        n_mels: the number of mel-frequencies
        frame_size: stft length
        hop_length: stft hop length
        sr: sample rate of audio
        to_db: convert to dB scale if `True` is given

    Returns:
        mel-spectrogram: [..., num_mel, T']
    """
    shape = audio.shape
    audio = audio.reshape(-1, shape[-1])
    mel_spec = librosa.feature.melspectrogram(y=audio, sr=sr, n_fft=frame_size + 1, hop_length=hop_length, n_mels=n_mels)
    mel_spec = mel_spec.transpose(0, 2, 1)
    mel_spec = mel_spec.reshape(shape[:-1] + mel_spec.shape[1:])
    if to_db:
        mel_spec = (librosa.power_to_db(mel_spec, ref=np.max) + 40) / 40
    return mel_spec


def _polyfit_val(mos, personalized):
    """use polyfit to convert raw mos values to DNSMOS values

    Args:
        mos: the raw mos values, [..., 4]
        personalized: whether interfering speaker is penalized

    Returns:
        DNSMOS: [..., 4]
    """
    if personalized:
        p_ovr = np.poly1d([-0.00533021, 0.005101, 1.18058466, -0.11236046])
        p_sig = np.poly1d([-0.01019296, 0.02751166, 1.19576786, -0.24348726])
        p_bak = np.poly1d([-0.04976499, 0.44276479, -0.1644611, 0.96883132])
    else:
        p_ovr = np.poly1d([-0.06766283, 1.11546468, 0.04602535])
        p_sig = np.poly1d([-0.08397278, 1.22083953, 0.0052439])  # x**2*v0 + x**1*v1+ v2
        p_bak = np.poly1d([-0.13166888, 1.60915514, -0.39604546])

    mos[..., 1] = p_sig(mos[..., 1])
    mos[..., 2] = p_bak(mos[..., 2])
    mos[..., 3] = p_ovr(mos[..., 3])
    return mos


def deep_noise_suppression_mean_opinion_score(x: Tensor, fs: int, personalized: bool, device: str = None):
    """Calculate `Deep Noise Suppression performance evaluation based on Mean Opinion Score`_ (DNSMOS).

    Human subjective evaluation is the ”gold standard” to evaluate speech quality optimized for human perception. 
    Perceptual objective metrics serve as a proxy for subjective scores. The conventional and widely used metrics
    require a reference clean speech signal, which is unavailable in real recordings. The no-reference approaches
    correlate poorly with human ratings and are not widely adopted in the research community. One of the biggest 
    use cases of these perceptual objective metrics is to evaluate noise suppression algorithms. DNSMOS generalizes 
    well in challenging test conditions with a high correlation to human ratings in stack ranking noise suppression 
    methods. More details can be found in [DNSMOS paper](https://arxiv.org/pdf/2010.15258.pdf).


    .. note:: using this metric requires you to have ``librosa`` and ``onnxruntime`` installed. Install as
        ``pip install librosa onnxruntime-gpu``.

    Args:
        x: [..., time]
        fs: sampling frequency
        personalized: whether interfering speaker is penalized
        device: the device used for calculating DNSMOS, can be cpu or cuda:n, where n is the index of gpu.
            If None is given, then the device of input is used.

    Returns:
        Float tensor with shape ``(..., 4)`` of DNSMOS values per sample, i.e. [p808_mos, mos_sig, mos_bak, mos_ovr]

    Raises:
        ModuleNotFoundError:
            If ``librosa`` or ``onnxruntime`` package is not installed

    Example:
        >>> from torch import randn
        >>> from torchmetrics.functional.audio.dnsmos import deep_noise_suppression_mean_opinion_score
        >>> g = torch.manual_seed(1)
        >>> preds = randn(8000)
        >>> deep_noise_suppression_mean_opinion_score(preds, 8000, False)
        tensor([2.1230, 1.8015, 1.1571, 1.2253], dtype=torch.float64)
    """
    if not _LIBROSA_AVAILABLE or not _ONNXRUNTIME_AVAILABLE:
        raise ModuleNotFoundError("DNSMOS metric requires that librosa and onnxruntime are installed."
                                  " Install as `pip install librosa onnxruntime-gpu`.")

    device = torch.device(device) if device is not None else x.device

    onnx_sess = _load_session(f"{DNSMOS_DIR}/{'p' if personalized else ''}DNSMOS/sig_bak_ovr.onnx", device)
    p808_onnx_sess = _load_session(f"{DNSMOS_DIR}/DNSMOS/model_v8.onnx", device)

    desired_fs = SAMPLING_RATE
    if fs != desired_fs:
        audio = librosa.resample(x.cpu().numpy(), orig_sr=fs, target_sr=desired_fs)
    else:
        audio = x.cpu().numpy()

    len_samples = int(INPUT_LENGTH * desired_fs)
    while len(audio) < len_samples:
        audio = np.concatenate([audio, audio], axis=-1)

    num_hops = int(np.floor(audio.shape[-1] / desired_fs) - INPUT_LENGTH) + 1

    moss = []
    hop_len_samples = desired_fs
    for idx in range(num_hops):
        audio_seg = audio[..., int(idx * hop_len_samples):int((idx + INPUT_LENGTH) * hop_len_samples)]
        if len(audio_seg) < len_samples:
            continue
        shape = audio_seg.shape
        audio_seg = audio_seg.reshape((-1, shape[-1]))

        input_features = np.array(audio_seg).astype('float32')
        p808_input_features = np.array(_audio_melspec(audio=audio_seg[..., :-160])).astype('float32')

        if device.type != 'cpu':
            input_features = ort.OrtValue.ortvalue_from_numpy(input_features, device.type, device.index)
            p808_input_features = ort.OrtValue.ortvalue_from_numpy(p808_input_features, device.type, device.index)

        oi = {'input_1': input_features}
        p808_oi = {'input_1': p808_input_features}
        mos = np.concatenate([p808_onnx_sess.run(None, p808_oi)[0], onnx_sess.run(None, oi)[0]], axis=-1, dtype='float64')
        mos = _polyfit_val(mos, personalized)

        mos = mos.reshape(shape[:-1] + (4,))
        moss.append(mos)
    mos = torch.from_numpy(np.mean(np.stack(moss, axis=-1), axis=-1))
    return mos  # [p808_mos, mos_sig, mos_bak, mos_ovr]
