import os
import joblib
import numpy as np
import soundfile as sf
import subprocess
import glob
from pathlib import Path
import librosa
import random
import tempfile

import pickle
import shutil
from lib.audiolib import audioread, audiowrite, normalize_single_channel_audio, audio_segmenter_4_file, \
    audio_segmenter_4_numpy, audio_energy_ratio_over_threshold, audio_energy_over_threshold, next_greater_power_of_2
from lib.utils import get_files_by_suffix, get_dirs_by_prefix, get_subdirs_by_suffix, get_subdirs_by_suffix, \
    get_subfiles_by_suffix, plot_curve
from ns_enhance_onnx import load_onnx_model, denoise_nsnet2
from ssl_feature_extractor import GccGenerator
from tqdm import tqdm
from threading import Thread
from multiprocessing import Process
from ssl_feature_extractor import audioFeatureExtractor

ref_audio, _ = audioread('../reference_wav.wav')
REF_AUDIO = normalize_single_channel_audio(ref_audio)
REF_AUDIO_THRESHOLD = (REF_AUDIO ** 2).sum() / len(REF_AUDIO) / 500
EPS = np.finfo(float).eps
REF_POWER = 1e-12
np.random.seed(0)


def decode_dir(d_path):
    '''
    get the basename of a path, and return the '_'-based split of it
    :param d_path:
    :return:
    '''
    return os.path.basename(d_path).split('_')


def decode_file_basename(file_path, seg=False):
    file_name = os.path.basename(os.path.normpath(file_path))
    file_split = file_name.split('_')[1:5]
    if seg:
        seg = [file_name[:-4].split('seg')[-1]]
        return list(map(float, file_split + seg))
    else:
        return list(map(float, file_split))


def print_info_of_walker_and_sound_source(dataset_path):
    print('-' * 20, 'info of smart walker', '-' * 20, )
    wk_dirs = get_dirs_by_prefix(dataset_path, 'walker_')
    wk_x, wk_y, wk_z, = set(), set(), set(),
    for i in wk_dirs:
        _, temp_x, temp_y, temp_z = decode_dir(i)
        wk_x.add(temp_x)
        wk_y.add(temp_y)
        wk_z.add(temp_z)
    wk_x, wk_y, wk_z, = list(map(int, wk_x)), list(map(int, wk_y)), list(map(int, wk_z)),
    print('x:', sorted(wk_x), '\n', 'y:', sorted(wk_y), '\n', 'z:', sorted(wk_z), )
    
    print('-' * 20 + 'info of sound source' + '-' * 20, )
    ss_dirs = get_dirs_by_prefix(dataset_path, 'src_')
    s_x, s_y, = set(), set(),
    for i in ss_dirs:
        _, temp_x, temp_y, = decode_dir(i)
        s_x.add(temp_x)
        s_y.add(temp_y)
    s_x, s_y, = list(map(int, s_x)), list(map(int, s_y)),
    print('x:', sorted(s_x), '\n', 'y:', sorted(s_y), )
    
    print('-' * 20 + 'info of direction of arrival (doa)' + '-' * 20, )
    wk_dirs = get_dirs_by_prefix(dataset_path, 'walker_')
    doa = set()
    for i in wk_dirs:
        subdirs = get_subdirs_by_suffix(i, )
        for j in subdirs:
            temp_doa = os.path.basename(j)
            doa.add(temp_doa)
    doa = list(map(int, doa))
    print('doa:', sorted(doa), )


def plot_map(dataset_path):
    '''
    原先为hole数据集而写，为了可视化房间地图，现在已经 be deprecated
    :param dataset_path:
    :return:
    '''
    dirs = get_dirs_by_prefix(dataset_path, 'src')
    
    for dir in dirs:
        print('\n', '-' * 20 + 'room_map' + '-' * 20, )
        
        arrows = ['->', '-°', '|^', '°-', '<-', '.|', '!!', '|.']
        files = get_files_by_suffix(dir, '.wav')
        [s_x, s_y] = list(map(float, os.path.basename(dir).split('_')[1:3]))
        print(s_x, s_y)
        
        room_map = np.ndarray((15, 19), dtype=object, )
        for i in files:
            temp = decode_file_basename(i)
            w_x = int(float(temp[0]) * 2) + 7
            w_z = int(float(temp[2]) * 2) + 9
            w_doa = int(temp[3]) // 45
            room_map[w_x, w_z] = arrows[w_doa]
        s_x = int(s_x * 2) + 7
        s_y = int(s_y * 2) + 9
        room_map[s_x, s_y] = 'oo'
        room_map = np.flip(room_map, axis=0)
        room_map = np.flip(room_map, axis=1)
        
        for i in range(len(room_map)):
            print(list(room_map[i]))


def clip_audio(src_dspath, des_dspath, seg_len, stepsize=1., fs=16000, window='hann', pow_2=False):
    '''
    Clip the audio into segments to segment_len in secs and save them into dir_name
    :param src_dspath: 长片段语音所在的数据集根目录
    :param des_dspath: 处理后数据集的根目录
    :param seg_len: clip 的长度 (单位为 s )
    :param stepsize: 相邻clip间跳过的比例dir_name
    :param fs: 采样率
    :param window: 为 clip 加窗
    :return: 无返回值
    '''
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    files = get_files_by_suffix(src_dspath, '.wav')
    
    def single_Process(files, ):
        for file in tqdm(files):
            # calculate the save_dpath
            src_doa_dir = os.path.dirname(file)
            rel_path = os.path.relpath(src_doa_dir, start=src_dspath, )
            dst_doa_dir = os.path.join(des_dspath, rel_path, )
            # print('src_doa_dir:', src_doa_dir)
            # print('dst_doa_dir:', dst_doa_dir)
            # segment the audio
            audio_segmenter_4_file(file, dst_doa_dir, segment_len=seg_len, stepsize=stepsize, fs=fs, window=window,
                                   padding=False, pow_2=pow_2, save2segFolders=True)
            
            # # 验证每一个seg folder中均有4通道信号
            # for sub_dpath in get_subdirs_by_suffix(dst_doa_dir):
            #     try:
            #         assert len(get_subfiles_by_suffix(sub_dpath, suffix='.wav')) == 4
            #     except Exception as e:
            #         print('e:', e)
    
    processes = []
    num_process = 32
    for i in range(num_process):
        processes.append(Process(target=single_Process, args=(files[i::num_process],)))
        processes[-1].start()
        # print(f'Process_{i} started', )
    for process in processes:
        process.join()


def denoise_audio_clips(dataset_root, dir_name, ini_ds_name, des_ds_name):
    '''Denoise the audio clips and save them into dir_name'''
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = os.path.join(dataset_root, dir_name, ini_ds_name)
    save_dspath = os.path.join(dataset_root, dir_name, des_ds_name)
    
    files = get_files_by_suffix(src_dspath, '.wav')
    
    def single_thread(files, ):
        model, _ = load_onnx_model(model_path='./ns_nsnet2-20ms-baseline.onnx')
        for file in tqdm(files):
            # calculate the save_dpath
            file_name, _ = os.path.splitext(os.path.basename(file))
            rel_path = os.path.relpath(file, start=src_dspath, )
            save_dpath = os.path.join(save_dspath, rel_path, )
            
            # denoise the audio
            denoise_nsnet2(audio_ipath=file, audio_opath=save_dpath, model=model, )
    
    processes = []
    for i in range(50):
        processes.append(Process(target=single_thread, args=(files[i::50],)))
        processes[-1].start()
        print('\nProcess %d has been created' % i)
    for process in processes:
        process.join()


def normalize_audio_clips(dataset_root, dir_name, ini_ds_name, des_ds_name):
    '''Denoise the audio clips and save them into dir_name'''
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = os.path.join(dataset_root, dir_name, ini_ds_name, )  # 'denoise_nsnet2'
    save_dspath = os.path.join(dataset_root, dir_name, des_ds_name)  # 'normalized_denoise_nsnet2'
    
    files = get_files_by_suffix(src_dspath, '.wav')
    
    def single_thread(files, ):
        for file in tqdm(files):
            # calculate the save_dpath
            file_name, _ = os.path.splitext(os.path.basename(file))
            rel_path = os.path.relpath(file, start=src_dspath, )
            save_dpath = os.path.join(save_dspath, rel_path, )
            
            # normalize the audio
            # denoise_nsnet2(audio_ipath=file, audio_opath=save_dpath, model=model, )
            audio, fs = audioread(file)
            audiowrite(save_dpath, audio, sample_rate=fs, norm=True, target_level=-25, clipping_threshold=0.99)
    
    processes = []
    for i in range(50):
        processes.append(Process(target=single_thread, args=(files[i::50],)))
        processes[-1].start()
        print('\nProcess %d has been created' % i)
    for process in processes:
        process.join()


def decode_audio_path(path):
    '''decode the info of the path of one audio
    ss: sound source
    wk: walker
    '''
    path = os.path.normpath(path)
    [wk_x, wk_z, wk_y, doa, seg] = decode_file_basename(path, seg=True)
    
    file_name = os.path.basename(os.path.dirname(os.path.dirname(path)))
    file_split = file_name.split('_')[1:3]
    [src_x, src_y] = list(map(float, file_split))
    
    return [src_x, src_y, wk_x, wk_y, wk_z, doa, seg]


def decode_src_name(src_name):
    return src_name.split('_')[1:3]


def decode_walker_name(walker_name, doa=True):
    if doa:
        return walker_name.split('_')[1:4], walker_name.split('_')[-1]
    elif not doa:
        return walker_name.split('_')[1:4]


def pack_data_into_dict(dataset_root, dir_name, ini_ds_name):
    '''pack the data into the dictionary form'''
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = os.path.join(dataset_root, dir_name, ini_ds_name)
    save_dspath = os.path.join(dataset_root, dir_name, ini_ds_name + '_dict.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    dataset = {}
    
    for src_name in tqdm(os.listdir(src_dspath)):
        # print('-' * 20, 'Processing ', src_name, '-' * 20, )
        src_key = '_'.join(decode_src_name(src_name))
        dataset[src_key] = {}
        for walker_name in os.listdir(os.path.join(src_dspath, src_name)):
            walker_key, doa = decode_walker_name(walker_name, doa=True)
            walker_key = '_'.join(walker_key)
            if walker_key in list(dataset[src_key].keys()):
                pass
            else:
                dataset[src_key][walker_key] = {}
            dataset[src_key][walker_key][doa] = {}
            
            dir_path = os.path.join(src_dspath, src_name, walker_name)
            files = get_files_by_suffix(dir_path, '.wav')
            seg_set = set([int(os.path.basename(i)[:-4].split('seg')[-1]) for i in files])
            # print(dir_path, '\n', seg_set)
            for seg in seg_set:
                seg_files = sorted(get_files_by_suffix(dir_path, 'seg' + str(seg) + '.wav'))
                # print(len(seg_files))
                seg_audio_list = []
                for file in seg_files:
                    audio, _ = audioread(file)
                    seg_audio_list.append(audio)
                dataset[src_key][walker_key][doa][str(seg)] = np.array([seg_audio_list])
    
    time_len, stepsize, threshold, fs = dir_name.split('_')
    save_ds = {
        'dataset'             : dataset,
        'fs'                  : int(fs),
        'time_len'            : time_len,
        'stepsize'            : str(stepsize) + 's',
        'threshold'           : threshold,
        'data_proprecess_type': '_'.join((dir_name, ini_ds_name)),
        'description'         : 'The dataset is organized as follows:\n\
                                dataset[src_key][wk_key][doa][str(seg)] = np.array([seg_audio_list])\n\
                                i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
                                src_key: src_x, src_y | wk_key: wk_x, wk_y, wk_z\n\
                                x, y, z: horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
                                fs: sample rate\n\
                                time_len: time length of one clip\n\
                                stepsize: stepsize when clipping the original audio\n\
                                threshold: threshold when dropping the clips\n\
                                data_proprecess_type: different preprocessing stage of this dataset'
    }
    with open(save_dspath, 'wb') as fo:
        pickle.dump(save_ds, fo, protocol=4)


def pack_data_into_array(dataset_root, dir_name, ini_ds_name):
    '''pack the data into the numpy-array form'''
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = os.path.join(dataset_root, dir_name, ini_ds_name)
    save_dspath = os.path.join(dataset_root, dir_name, ini_ds_name + '_np.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    ds_audio_list, ds_label_list = [], []
    for src_name in tqdm(os.listdir(src_dspath)):
        # print('-' * 20, 'Processing ', src_name, '-' * 20, )
        src_audio_list, src_label_list = [], []
        for walker_name in os.listdir(os.path.join(src_dspath, src_name)):
            dir_path = os.path.join(src_dspath, src_name, walker_name)
            files = get_files_by_suffix(dir_path, '.wav')
            seg_set = set([int(os.path.basename(i)[:-4].split('seg')[-1]) for i in files])
            # print(dir_path, '\n', seg_set)
            for i in seg_set:
                seg_files = sorted(get_files_by_suffix(dir_path, 'seg' + str(i) + '.wav'))
                # print(len(seg_files))
                seg_audio_list, seg_label_list = [], []
                for file in seg_files:
                    # print(os.path.basename(file))
                    audio, _ = audioread(file)
                    seg_audio_list.append(audio)
                    seg_label_list.append(decode_audio_path(file))  # decode the info of the audio
                
                src_audio_list.append([seg_audio_list])
                src_label_list.append(seg_label_list[0])
        ds_audio_list.append(np.array(src_audio_list))
        ds_label_list.append(np.array(src_label_list))
    ds_audio_array = np.array(ds_audio_list, dtype=object)
    ds_label_array = np.array(ds_label_list, dtype=object)
    
    time_len, stepsize, threshold, fs = dir_name.split('_')
    
    dataset = {
        'x'                   : ds_audio_array,
        'y'                   : ds_label_array,
        'fs'                  : int(fs),
        'time_len'            : time_len,
        'stepsize'            : str(stepsize) + 's',
        'threshold'           : threshold,
        'data_proprecess_type': '_'.join((dir_name, ini_ds_name)),
        'description'         : 'The first dimension of this dataset is organized based on the locations of different sound sources. \n\
                                And the following dimensions are sample_number * 1 * microphones (i.e. 4) * sample_points. \n\
                                The label is [src_x, src_y, wk_x, wk_y, wk_z, doa, seg] \n\
                                i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
                                x , y , z:  horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
                                fs: sample rate\n\
                                time_len: time length of one clip\n\
                                stepsize: overlap_ratio when clipping the original audio\n\
                                threshold: threshold when dropping the clips\n\
                                data_proprecess_type: different preprocessing stage of this dataset'
    }
    with open(save_dspath, 'wb') as fo:
        pickle.dump(dataset, fo, protocol=4)


def next_lower_power_of_2(x):
    return 2 ** ((int(x)).bit_length() - 1)


def extract_gcc_phat_from_np(dataset_path, fs, fft_len, feature_len=128):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = dataset_path
    save_dspath = os.path.join(src_dspath[:-4] + '_gcc_phat_' + str(feature_len) + '.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    with open(src_dspath, 'rb') as fo:
        dataset = pickle.load(fo)
    
    x = dataset['x']
    feature_extractor = audioFeatureExtractor(fs=fs, fft_len=fft_len, num_gcc_bin=feature_len, num_mel_bin=feature_len,
                                         datatype='mic', )
    gcc_ds_ls = []
    for i in range(len(x)):
        gcc_src_ls = []
        for j in range(len(x[i])):
            audio_ls = x[i][j]
            gcc_seg_ls = feature_extractor.get_gcc_phat(audio=audio_ls)
            gcc_src_ls.append([gcc_seg_ls])
        gcc_ds_ls.append(np.array(gcc_src_ls))
    gcc_ds_array = np.array(gcc_ds_ls, dtype=object)
    
    gcc_dataset = {
        'x'                   : gcc_ds_array,
        'y'                   : dataset['y'],
        'fs'                  : dataset['fs'],
        'time_len'            : dataset['time_len'],
        'stepsize'            : dataset['stepsize'],
        'feature_len'         : feature_len,
        'threshold'           : dataset['threshold'],
        'data_proprecess_type': dataset['data_proprecess_type'],
        'description'         : 'The first dimension of this dataset is organized based on the location of different sound sources. \n\
        And the following dimensions are sample_number * 1* combinations (the different combinations of two microphones, i.e. C*2_4 = 6) * gcc_features( i.e. 128). \n\
        The label is [src_x, src_y, wk_x, wk_y, wk_z, doa, seg] \n\
         i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
         x , y , z:  horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
         fs: sample rate\n\
         time_len: time length of one clip\n\
         stepsize: overlap_ratio when clipping the original audio\n\
         feature_len: length of feature\n\
         threshold: threshold when dropping the clips\n\
         data_proprecess_type: different preprocessing stage of this dataset'
    }
    with open(save_dspath, 'wb') as fo:
        pickle.dump(gcc_dataset, fo, protocol=4)


def extract_gcc_phat_from_dict(dataset_path, fs, fft_len, feature_len=128):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = dataset_path
    save_dspath = os.path.join(src_dspath[:-4] + '_gcc_phat_' + str(feature_len) + '.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    with open(src_dspath, 'rb') as fo:
        dataset = pickle.load(fo)
    
    gcc_dataset = dataset['dataset']
    feature_extractor = audioFeatureExtractor(fs=fs, fft_len=fft_len, num_gcc_bin=feature_len, num_mel_bin=feature_len,
                                         datatype='mic', )
    for src_key in list(gcc_dataset.keys()):
        for wk_key in list(gcc_dataset[src_key].keys()):
            for doa_key in list(gcc_dataset[src_key][wk_key].keys()):
                for seg_key in list(gcc_dataset[src_key][wk_key][doa_key].keys()):
                    audio_ls = gcc_dataset[src_key][wk_key][doa_key][seg_key]
                    gcc_seg_ls = feature_extractor.get_gcc_phat(audio=audio_ls)
                    gcc_dataset[src_key][wk_key][doa_key][seg_key] = gcc_seg_ls
    
    gcc_dataset = {
        'dataset'             : gcc_dataset,
        'fs'                  : dataset['fs'],
        'time_len'            : dataset['time_len'],
        'stepsize'            : dataset['stepsize'],
        'feature_len'         : feature_len,
        'threshold'           : dataset['threshold'],
        'data_proprecess_type': dataset['data_proprecess_type'],
        'description'         : 'The dataset is organized as follows:\n\
                                dataset[src_key][wk_key][doa][str(seg)] = np.array([seg_gcc_phat])\n\
                                i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
                                src_key: src_x, src_y | wk_key: wk_x, wk_y, wk_z\n\
                                x, y, z: horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
                                fs: sample rate\n\
                                time_len: time length of one clip\n\
                                stepsize: stepsize when clipping the original audio\n\
                                threshold: threshold when dropping the clips\n\
                                data_proprecess_type: different preprocessing stage of this dataset'
    }
    with open(save_dspath, 'wb') as fo:
        pickle.dump(gcc_dataset, fo, protocol=4)


def extract_log_mel_from_np(dataset_path, fs, fft_len, feature_len=128):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = dataset_path
    save_dspath = os.path.join(src_dspath[:-4] + '_log_mel_' + str(feature_len) + '.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    with open(src_dspath, 'rb') as fo:
        dataset = pickle.load(fo)
    
    x = dataset['x']
    feature_extractor = audioFeatureExtractor(fs=fs, fft_len=fft_len, num_gcc_bin=feature_len, num_mel_bin=feature_len,
                                         datatype='mic', )  # fft_len need to be modified
    mel_ds_ls = []
    for i in range(len(x)):
        mel_src_ls = []
        for j in range(len(x[i])):
            audio_ls = x[i][j]
            mel_seg_ls = feature_extractor.get_log_mel(audio=audio_ls)
            mel_src_ls.append([mel_seg_ls])
        mel_ds_ls.append(np.array(mel_src_ls))
    mel_ds_array = np.array(mel_ds_ls, dtype=object)
    
    mel_dataset = {
        'x'                   : mel_ds_array,
        'y'                   : dataset['y'],
        'fs'                  : dataset['fs'],
        'time_len'            : dataset['time_len'],
        'stepsize'            : dataset['stepsize'],
        'feature_len'         : feature_len,
        'threshold'           : dataset['threshold'],
        'data_proprecess_type': dataset['data_proprecess_type'],
        'description'         : 'The first dimension of this dataset is organized based on the location of different sound sources. \n\
        And the following dimensions are sample_number * 1* combinations (the different combinations of two microphones, i.e. C*2_4 = 6) * log_mel_features( i.e. 128). \n\
        The label is [src_x, src_y, wk_x, wk_y, wk_z, doa, seg] \n\
         i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
         x , y , z:  horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
         fs: sample rate\n\
         time_len: time length of one clip\n\
         stepsize: overlap_ratio when clipping the original audio\n\
         feature_len: length of feature\n\
         threshold: threshold when dropping the clips\n\
         data_proprecess_type: different preprocessing stage of this dataset'
    }
    with open(save_dspath, 'wb') as fo:
        pickle.dump(mel_dataset, fo, protocol=4)


def extract_log_mel_from_dict(dataset_path, fs, fft_len, feature_len=128):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = dataset_path
    save_dspath = os.path.join(src_dspath[:-4] + '_log_mel_' + str(feature_len) + '.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    with open(src_dspath, 'rb') as fo:
        dataset = pickle.load(fo)
    
    mel_dataset = dataset['dataset']
    feature_extractor = audioFeatureExtractor(fs=fs, fft_len=fft_len, num_gcc_bin=feature_len, num_mel_bin=feature_len,
                                         datatype='mic', )
    for src_key in list(mel_dataset.keys()):
        for wk_key in list(mel_dataset[src_key].keys()):
            for doa_key in list(mel_dataset[src_key][wk_key].keys()):
                for seg_key in list(mel_dataset[src_key][wk_key][doa_key].keys()):
                    audio_ls = mel_dataset[src_key][wk_key][doa_key][seg_key]
                    mel_seg_ls = feature_extractor.get_log_mel(audio=audio_ls)
                    mel_dataset[src_key][wk_key][doa_key][seg_key] = mel_seg_ls
    
    mel_dataset = {
        'dataset'             : mel_dataset,
        'fs'                  : dataset['fs'],
        'time_len'            : dataset['time_len'],
        'stepsize'            : dataset['stepsize'],
        'feature_len'         : feature_len,
        'threshold'           : dataset['threshold'],
        'data_proprecess_type': dataset['data_proprecess_type'],
        'description'         : 'The dataset is organized as follows:\n\
                                dataset[src_key][wk_key][doa][str(seg)] = np.array([seg_log_mel])\n\
                                i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
                                src_key: src_x, src_y | wk_key: wk_x, wk_y, wk_z\n\
                                x, y, z: horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
                                fs: sample rate\n\
                                time_len: time length of one clip\n\
                                stepsize: stepsize when clipping the original audio\n\
                                threshold: threshold when dropping the clips\n\
                                data_proprecess_type: different preprocessing stage of this dataset'
    }
    
    with open(save_dspath, 'wb') as fo:
        pickle.dump(mel_dataset, fo, protocol=4)


def extract_STFT_from_np(dataset_path, fs, clip_ms_length=64, overlap_ratio=0.5):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = dataset_path
    save_dspath = os.path.join(src_dspath[:-4] + '_STFT' +
                               '_clip_ms_' + str(clip_ms_length) + '_overlap_' + str(round(overlap_ratio, 2)) + '.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    with open(src_dspath, 'rb') as fo:
        dataset = pickle.load(fo)
    
    x = dataset['x']
    feature_extractor = audioFeatureExtractor(fs=fs, datatype='mic', )  # fft_len need to be modified
    stft_ds_ls = []
    for i in range(len(x)):
        stft_src_ls = []
        for j in range(len(x[i])):
            audio_ls = x[i][j]
            stft_seg_ls = feature_extractor.get_STFT(audio_ls, clip_ms_length, overlap_ratio)
            stft_src_ls.append(stft_seg_ls)
        stft_ds_ls.append(np.array(stft_src_ls))
    stft_ds_array = np.array(stft_ds_ls, dtype=object)
    
    stft_dataset = {
        'x'                   : stft_ds_array,
        'y'                   : dataset['y'],
        'fs'                  : dataset['fs'],
        'time_len'            : dataset['time_len'],
        'stepsize'            : dataset['stepsize'],
        'clip_ms_length'      : clip_ms_length,
        'overlap_ratio'       : overlap_ratio,
        'threshold'           : dataset['threshold'],
        'data_proprecess_type': dataset['data_proprecess_type'],
        'description'         : 'The first dimension of this dataset is organized based on the location of different sound sources. \n\
        And the following dimensions are sample_number * 1* combinations (the different combinations of two microphones, i.e. C*2_4 = 6) * stft_features( i.e. 128). \n\
        The label is [src_x, src_y, wk_x, wk_y, wk_z, doa, seg] \n\
         i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
         x , y , z:  horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
         fs: sample rate\n\
         time_len: time length of one clip\n\
         stepsize: overlap_ratio when clipping the original audio\n\
         clip_ms_length: length of clip when calculating STFT\n\
         overlap_ratio: overlap_ratio when clipping audios for STFT \n\
         threshold: threshold when dropping the clips\n\
         data_proprecess_type: different preprocessing stage of this dataset'
    }
    with open(save_dspath, 'wb') as fo:
        pickle.dump(stft_dataset, fo, protocol=4)


def extract_STFT_from_dict(dataset_path, fs, clip_ms_length=64, overlap_ratio=0.5):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = dataset_path
    save_dspath = os.path.join(src_dspath[:-4] + '_STFT' +
                               '_clip_ms_' + str(clip_ms_length) + '_overlap_' + str(round(overlap_ratio, 2)) + '.pkl')
    
    save_dspath = os.path.join(src_dspath[:-4] + '_log_mel_' + str(feature_len) + '.pkl')
    os.makedirs(os.path.dirname(save_dspath), exist_ok=True)
    
    with open(src_dspath, 'rb') as fo:
        dataset = pickle.load(fo)
    
    mel_dataset = dataset['dataset']
    feature_extractor = audioFeatureExtractor(fs=fs, fft_len=fft_len, num_gcc_bin=feature_len, num_mel_bin=feature_len,
                                         datatype='mic', )
    for src_key in list(mel_dataset.keys()):
        for wk_key in list(mel_dataset[src_key].keys()):
            for doa_key in list(mel_dataset[src_key][wk_key].keys()):
                for seg_key in list(mel_dataset[src_key][wk_key][doa_key].keys()):
                    audio_ls = mel_dataset[src_key][wk_key][doa_key][seg_key]
                    mel_seg_ls = feature_extractor.get_log_mel(audio=audio_ls)
                    mel_dataset[src_key][wk_key][doa_key][seg_key] = mel_seg_ls
    
    mel_dataset = {
        'dataset'             : mel_dataset,
        'fs'                  : dataset['fs'],
        'time_len'            : dataset['time_len'],
        'stepsize'            : dataset['stepsize'],
        'feature_len'         : feature_len,
        'threshold'           : dataset['threshold'],
        'data_proprecess_type': dataset['data_proprecess_type'],
        'description'         : 'The dataset is organized as follows:\n\
                                dataset[src_key][wk_key][doa][str(seg)] = np.array([seg_log_mel])\n\
                                i.e. src: sound source | wk: walker | doa: direction of arrival | seg: segment number of the audio clip in the original long audio | \n\
                                src_key: src_x, src_y | wk_key: wk_x, wk_y, wk_z\n\
                                x, y, z: horizontal coordinate! , vertical coordinate! , hight of the walker (always be 1 in this dataset)\n\
                                fs: sample rate\n\
                                time_len: time length of one clip\n\
                                stepsize: stepsize when clipping the original audio\n\
                                threshold: threshold when dropping the clips\n\
                                data_proprecess_type: different preprocessing stage of this dataset'
    }
    
    with open(save_dspath, 'wb') as fo:
        pickle.dump(mel_dataset, fo, protocol=4)


def drop_audio_clips(dataset_root, dir_name, ini_ds_name, des_ds_name, fs, threshold):
    '''Denoise the audio clips and save them into dir_name'''
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    
    src_dspath = os.path.join(dataset_root, dir_name, ini_ds_name, )  # 'denoise_nsnet2'
    save_dspath = os.path.join(dataset_root, dir_name, des_ds_name)  # 'normalized_denoise_nsnet2'
    
    files = get_files_by_suffix(src_dspath, '.wav')
    
    def single_thread(files, ):
        for file in tqdm(files):
            # calculate the save_dpath
            file_name, _ = os.path.splitext(os.path.basename(file))
            rel_path = os.path.relpath(file, start=src_dspath, )
            save_dpath = os.path.join(save_dspath, rel_path, )
            
            # normalize the audio
            # denoise_nsnet2(audio_ipath=file, audio_opath=save_dpath, model=model, )
            audio, fs = audioread(file)
            if audio_energy_over_threshold(audio, threshold=REF_AUDIO_THRESHOLD) and \
                    audio_energy_ratio_over_threshold(audio, fs=fs, threshold=threshold, ):
                os.makedirs(os.path.dirname(save_dpath), exist_ok=True)
                shutil.copy(file, save_dpath)
            else:
                continue
    
    processes = []
    for i in range(20):
        processes.append(Process(target=single_thread, args=(files[i::20],)))
        processes[-1].start()
    for process in processes:
        process.join()


def preprocessing_audio_with_norm_denoise_drop(src_dspath, fs=16000, threshold=None):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    files = get_files_by_suffix(src_dspath, '.wav')
    
    def single_Process(files, ):
        denoise_model, _ = load_onnx_model(model_path='./ns_nsnet2-20ms-baseline.onnx')
        for fpath in tqdm(files):
            ini_audio, ini_fs = audioread(fpath)
            assert ini_fs == fs
            
            audio = np.array(ini_audio)
            # norm
            norm_audio, norm_scalar = normalize_single_channel_audio(audio, returnScalar=True)
            # denoise
            de_norm_audio = denoise_nsnet2(audio=norm_audio, fs=fs, model=denoise_model, )
            # drop
            if audio_energy_over_threshold(de_norm_audio, threshold=REF_AUDIO_THRESHOLD) and \
                    audio_energy_ratio_over_threshold(de_norm_audio, fs=fs, threshold=threshold, ):
                doDrop = False
            else:
                doDrop = True
            
            if not doDrop:
                dst_fpath = fpath.replace('ini_hann', 'ini_hann_norm_denoise_drop')
                assert dst_fpath != fpath
                os.makedirs(os.path.dirname(dst_fpath), exist_ok=True)
                de_audio = de_norm_audio / norm_scalar
                audiowrite(destpath=dst_fpath, audio=de_audio, sample_rate=fs, norm=False, clipping_threshold=None, )
    
    processes = []
    num_process = 128
    for i in range(num_process):
        processes.append(Process(target=single_Process, args=(files[i::num_process],)))
        processes[-1].start()
        # print(f'Process_{i} started', )
    for process in processes:
        process.join()


def clean_audio_clips(ds_path):
    files = get_files_by_suffix(ds_path, '.wav')
    
    def single_Process(files, ):
        for fpath in tqdm(files):
            seg_dir = os.path.dirname(fpath)
            seg_files = get_files_by_suffix(seg_dir, '.wav')
            if len(seg_files) < 4:
                try:
                    shutil.rmtree(seg_dir, ignore_errors=True, )
                    print('seg_dir:', seg_dir)
                except:
                    pass
    
    processes = []
    num_process = 64
    for i in range(num_process):
        processes.append(Process(target=single_Process, args=(files[i::num_process],)))
        processes[-1].start()
        # print(f'Process_{i} started', )
    for process in processes:
        process.join()


def preprocessing_audio_with_norm_denoise_drop_norm(src_dspath):
    'dataset -> ds  ;  data -> dt  ;    file -> f  ;   dir -> d  '
    files = get_files_by_suffix(src_dspath, '.wav')
    
    def single_Process(files, ):
        for fpath in tqdm(files):
            ini_audio, ini_fs = audioread(fpath)
            assert ini_fs == fs
            dst_fpath = fpath.replace('ini_hann_norm_denoise_drop', 'ini_hann_norm_denoise_drop_norm')
            assert dst_fpath != fpath
            
            audiowrite(destpath=dst_fpath, audio=ini_audio, sample_rate=ini_fs, norm=True, clipping_threshold=None, )
    
    processes = []
    num_process = 64
    for i in range(num_process):
        processes.append(Process(target=single_Process, args=(files[i::num_process],)))
        processes[-1].start()
        # print(f'Process_{i} started', )
    for process in processes:
        process.join()


if __name__ == '__main__':
    print('-' * 20 + 'Preprocessing the dateset' + '-' * 20)
    dataset_root = '../dataset/4F_CYC'
    dataset_ini = os.path.join(dataset_root, 'initial')
    
    print_info_of_walker_and_sound_source(dataset_ini)
    
    segment_para_set = {
        '32ms' : {
            'name'     : '32ms',
            'time_len' : 32 / 1000,
            'threshold': 100,
            'stepsize' : 0.5
        },
        '50ms' : {
            'name'     : '50ms',
            'time_len' : 50 / 1000,
            'threshold': 100,
            'stepsize' : 0.5
        },
        '64ms' : {
            'name'     : '64ms',
            'time_len' : 64 / 1000,
            'threshold': 100,
            'stepsize' : 0.5
        },
        '128ms': {
            'name'     : '128ms',
            'time_len' : 128 / 1000,
            'threshold': 200,  # 100?
            'stepsize' : 0.5
        },
        '256ms': {
            'name'     : '256ms',
            'time_len' : 256 / 1000,
            'threshold': 400,
            'stepsize' : 256 / 1000 / 2
        },
        '1s'   : {
            'name'     : '1s',
            'time_len' : 1,
            'threshold': 800,
            'stepsize' : 0.5,
        },
    }
    fs = 16000
    window = 'hann'
    assert window == 'hann'  # required by the following code
    clip_len = '1s'
    seg_para = segment_para_set[clip_len]
    pow_2 = False
    seg_ds_name = '_'.join([seg_para['name'], str(round(seg_para['stepsize'], 2)), str(seg_para['threshold']), str(fs)])
    seg_len = int(seg_para['time_len'] * fs)
    if pow_2:
        seg_len = next_greater_power_of_2(seg_len)
    step_size = int(seg_len * seg_para['stepsize'])
    fft_len = seg_len
    print('-' * 20 + 'parameters' + '-' * 20, '\n', seg_para)
    print('seg_ds_name:', seg_ds_name)
    print('Actual para:\n', 'seg_len:', seg_len, '\n', 'step_size:', step_size, '\n', 'fft_len:', fft_len, )
    
    initial_dspath = os.path.join(dataset_root, 'initial')
    
    ini_seg_dspath = os.path.join(dataset_root, seg_ds_name, 'ini_' + window)
    # print('Start seg...')
    # clip_audio(initial_dspath, ini_seg_dspath, seg_para['time_len'], seg_para['stepsize'], fs, window=window,
    #            pow_2=pow_2, )
    # print('Finish seg...')
    
    norm_denoise_drop_dspath = ini_seg_dspath.replace('ini_hann', 'ini_hann_norm_denoise_drop')
    # print('Start norm_denoise_drop...')
    # preprocessing_audio_with_norm_denoise_drop(src_dspath=ini_seg_dspath, fs=fs, threshold=float(seg_para['threshold']))
    # print('Finish norm_denoise_drop...')
    
    # print('Start cleaning...')
    # clean_audio_clips(ds_path=norm_denoise_drop_dspath)
    # print('Finish cleaning...')
    
    norm_denoise_drop_norm_dspath = \
        ini_seg_dspath.replace('ini_hann_norm_denoise_drop', 'ini_hann_norm_denoise_drop_norm')
    # print('Start norm_denoise_drop_norm...')
    # preprocessing_audio_with_norm_denoise_drop_norm(src_dspath=norm_denoise_drop_dspath, )
    # print('Finish norm_denoise_drop_norm...')
    
    ''' extract features '''
    
    print('Start gcc...')
    extract_gcc_phat_from_np(dataset_path=dataset_path, fs=fs, fft_len=fft_len, feature_len=128)
    print('Finish gcc...')
    
    # print('Start log_mel...')
    # extract_log_mel_from_dict(dataset_path=dataset_path, fs=fs, fft_len=fft_len, feature_len=128)
    # print('Finish log_mel...')
    
    # print('Start STFT...')
    # extract_STFT_from_np(dataset_path=dataset_path, fs=fs, )
    print('Finish STFT...')
