'''
Class to produce metadata for root to numpy conversion.

@author: hqu
'''

from __future__ import print_function

import os
import re
import json
import logging
import numpy as np
import pandas as pd

from helper import get_num_events

class Metadata(object):

    ''' Compile the metadata. '''

    def __init__(self,
                 inputdir,
                 treename='deepntuplizer/tree',
                 reweight_events=100000,
                 reweight_bins=[200, 250, 300, 350, 400, 450, 500, 550, 600, 650,
                                700, 800, 900, 1000, 1100,
                                1200, 1400, 1600, 5000],
                 metadata_events=100000,
                 selection=None,
                 var_groups=None,  # {group_name:(regex, num)}
                 var_blacklist=None,
                 var_no_transform_branches=None,
                 label_list=None,
                 reweight_var='fj_pt',
                 var_img='pfcand_ptrel',
                 var_pos=['pfcand_etarel', 'pfcand_phirel'],
                 n_pixels=64,
                 img_ranges=[[-0.8, 0.8], [-0.8, 0.8]]
                 ):
        self._inputdir = inputdir  # data members starting with '_' is not loaded from json

        self.treename = treename
        self.reweight_var = reweight_var
        self._reweight_events = reweight_events
        self._reweight_bins = reweight_bins
        self._metadata_events = metadata_events
        self.selection = selection
        self.var_groups = var_groups
        self.var_blacklist = var_blacklist
        self.var_no_transform_branches = var_no_transform_branches
        self.label_branches = label_list
        
        self.var_img = var_img
        self.var_pos = var_pos
        self.n_pixels = n_pixels
        self.img_ranges = img_ranges

        self.inputfiles = None
        self.num_events = None

    def produceMetadata(self, filepath):
        logging.info('Start producing metadata...')
        # make file list
        self.updateFilelist()
        # make var list
        self._make_varlist()
        # make weights
        self._make_weights()
        # make transfromation info
        self._make_infos()
        # write metadata
        self.writeMetadata(filepath)

    def loadMetadata(self, filepath):
        with open(filepath) as metafile:
            md = json.load(metafile, encoding='ascii')
            for k in md:
                if k.startswith('_'): continue
                setattr(self, k, md[k])
        logging.info('Metadata loaded from ' + filepath)

    def updateFilelist(self, test_sample=False):
        self.inputfiles = []
        self.num_events = []
        for dp, dn, filenames in os.walk(self._inputdir):
            if 'failed' in dp or 'ignore' in dp:
                continue
            if not test_sample and 'test_sample' in dp:
                # train/val samples
                continue
            if test_sample and 'test_sample' not in dp:
                # test samples
                continue
            for f in filenames:
                if not f.endswith('.root'):
                    continue
                fullpath = os.path.join(dp, f)
                nevts = get_num_events(fullpath, self.treename)
                if nevts:
                    self.inputfiles.append(fullpath)
                    self.num_events.append(nevts)
                else:
                    logging.warning('Ignore erroneous file %s' % fullpath)
        self._total_events = sum(self.num_events)
        logging.info('Created file list from directory %s\nFiles:%d, Events:%d' % (self._inputdir, len(self.inputfiles), self._total_events))
        return (self.inputfiles, self.num_events)

    def writeMetadata(self, filepath):
        with open(filepath, 'w') as metafile:
            json.dump(self.__dict__, metafile, indent=2, encoding='ascii', sort_keys=True)
        logging.info('Metadata written to ' + filepath)


    def _make_varlist(self):
        # get all branches and filter them using input variable list
        from root_numpy import root2array
        df = pd.DataFrame(root2array(self.inputfiles[0], treename=self.treename, stop=1))
        self._all_branches = df.columns.values.tolist()
        self.var_branches = []
        self.var_sizes = {}
        for k in self._all_branches:
            matched = False
            for v_group in self.var_groups:
                size = self.var_groups[v_group][1]
                for regex in self.var_groups[v_group][0]:
                    if re.match(regex, k):
                        self.var_branches.append(k)
                        self.var_sizes[k] = size
                        matched = True
                        break
                if matched: break
        for var in self.var_blacklist + self.label_branches:
            try:
                self.var_branches.remove(var)
            except ValueError:
                pass
        logging.info('Training vars:\n' + '\n'.join(self.var_branches))

    def _prepare_reweight_info(self, rec):
        ''' Produce metadata for reweighting. Goal:
            1) Produce flat pT spectrum.
            2) Balance the class weights on top of that
        '''
        class_events = {}
        result = {}
        for label in self.label_branches:
            pos = (rec[label] == 1)
            a = rec[self.reweight_var][pos]
#             class_events[label] = 0
            hist, bin_edges = np.histogram(a, bins=self._reweight_bins, range=(min(self._reweight_bins), max(self._reweight_bins)))
            hist = np.asfarray(hist, dtype=np.float32)
            result[label] = {'bin_edges':bin_edges.tolist(), 'hist':hist[:], 'raw_hist':hist[:].tolist()}
            logging.debug('%s:\n%s' % (label, str(hist)))
            if min(hist[-2:]) < 10:
#                 logging.warning('Not enough events for cateogry %s:\n%s' % (label, str(hist)))
                raise RuntimeError('Not enough events for cateogry %s:\n%s' % (label, str(hist)))
            ref_val = np.min([x for x in hist if x > 0])
            class_events[label] = ref_val
            for i in range(len(hist)):
                if hist[i] != 0:
                    result[label]['hist'][i] = ref_val / hist[i]
        min_nevt = min(class_events.values())
        for label in self.label_branches:
            class_wgt = float(min_nevt) / class_events[label]
            result[label]['class_wgt'] = class_wgt
            result[label]['hist'] = result[label]['hist'].tolist()
        return result

    def _make_weights(self):
        # fraction of events to take from each file
        from root_numpy import root2array
        frac = 1.0
        if self._reweight_events > 0:
            frac = float(self._reweight_events) / self._total_events
        if frac < 1:
            pieces = []
            for fn, n in zip(self.inputfiles, self.num_events):
                a = root2array(fn, treename=self.treename, selection=self.selection, stop=int(frac * n),
                               branches=self.label_branches + [self.reweight_var])
                pieces.append(a)
            rec = np.concatenate(pieces)
        else:
            rec = root2array(self.inputfiles, treename=self.treename, selection=self.selection,
                               branches=self.label_branches + [self.reweight_var])
        logging.info('Use %d events to produce reweight info' % rec.shape[0])
        # get distribution for reweighting
        self.reweight_info = self._prepare_reweight_info(rec)
        logging.debug('Reweight info:\n' + str(self.reweight_info))

    def _make_infos(self):
        # make variables transformation infos
        from root_numpy import root2array
        frac = 1.0
        _inputfiles = self.inputfiles
        _num_events = self.num_events
        if self._metadata_events > 0:
            nfiles = int(5 * float(self._metadata_events) / self._total_events * len(self.inputfiles))
            file_inds = np.arange(len(self.inputfiles))
            np.random.shuffle(file_inds)
            file_inds = file_inds[:nfiles]
            _inputfiles = [self.inputfiles[i] for i in file_inds]
            _num_events = [self.num_events[i] for i in file_inds]
            frac = float(self._metadata_events) / sum(_num_events)

        first = True

        self.branches_info = {}
        for var in self.var_branches:
            var_size = self.var_sizes[var]
            pieces = []
            for fn, n in zip(_inputfiles, _num_events):
                v = root2array(fn, treename=self.treename, selection=self.selection,
                               branches=var, stop=int(frac * n))
                pieces.append(v)
            a = np.concatenate(pieces)
            if first:
                first = False
                logging.debug('Use %d events for var transform info' % a.shape[0])
            size = None
            if a.dtype == np.object:
                if var_size:
                    size = var_size  # use given size if provided
                else:
                    lengths = [len(row) for row in a]
                    size = int(round(np.percentile(lengths, 95)))  # else get 95% percentile of the length
                a = np.nan_to_num(np.concatenate(a))  # then flatten vector vars for calculations
            self.branches_info[var] = {
                'size'  : size,
                'median': float(np.percentile(a, 50)),  # need float otherwise cannot serialize to json
                'upper' : float(np.percentile(a, 84)),
                'min'   : float(np.min(a)),
                'max'   : float(np.max(a)),
                'mean'  : float(np.mean(a)),
                'std'   : float(np.std(a)),
                }
            logging.debug(var + ': ' + str(self.branches_info[var]))