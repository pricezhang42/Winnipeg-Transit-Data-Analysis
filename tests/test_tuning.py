import copy
import json
from pathlib import Path
import unittest
import pandas as pd
from src.tune import fold_frames, rank_candidates, validate_config


class TuningTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path('tuning_config.json').read_text())

    def test_overlapping_validation_and_holdout_are_rejected(self):
        validate_config(self.config)
        bad = copy.deepcopy(self.config)
        bad['folds'][1]['validation_start'] = '2026-08-16'
        with self.assertRaises(ValueError):
            validate_config(bad)
        bad = copy.deepcopy(self.config)
        bad['holdout_start'] = '2026-09-01'
        with self.assertRaises(ValueError):
            validate_config(bad)

    def test_fold_excludes_future_and_includes_flagged_validation(self):
        frame = pd.DataFrame({'row_id':['1','2','3','4','5'],
            'scheduled_time_local':pd.to_datetime(['2026-08-09','2026-08-09','2026-08-10','2026-08-16','2026-08-17']),
            'baseline_eligible':[True,False,True,False,True], 'valid_required_fields':[True]*5})
        train, validation = fold_frames(frame, self.config, self.config['folds'][0])
        self.assertEqual(train.row_id.tolist(), ['1'])
        self.assertEqual(validation.row_id.tolist(), ['3','4'])

    def test_selection_and_refit_iterations_use_validation_only(self):
        candidates = [{'name':'a'},{'name':'reference'}]
        records = [{'candidate':name,'validation':{'mae_seconds':score},'tree_count':trees,
                    'holdout_mae_seconds':999 if name=='a' else 1}
                   for name,score,trees in [('a',10,100),('a',12,200),('a',11,900),('reference',15,50)]]
        ranking = rank_candidates(records, candidates)
        self.assertEqual(ranking[0]['candidate'], 'a')
        self.assertEqual(ranking[0]['final_tree_count'], 200)
        self.assertEqual(ranking[0]['mean_validation_mae_seconds'], 11)
