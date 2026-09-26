import unittest
from passup_policy import category, classify, POLICY
class PolicyTests(unittest.TestCase):
    def test_sample_and_zero_report_gates(self):
        self.assertEqual(category(59,20,1,1),'unknown')
        self.assertEqual(category(200,9,0,0),'unknown')
        self.assertEqual(category(119,10,0,0),'unknown')
        self.assertEqual(category(120,10,0,0),'low')
    def test_boundaries_and_persistence(self):
        self.assertEqual(category(150,10,2,2),'medium')
        self.assertEqual(category(150,10,2,1),'unknown')
        self.assertEqual(category(70,10,3,3),'high')
        self.assertEqual(category(70,10,3,2),'unknown')
        self.assertEqual(category(200,10,1,1),'low')
    def test_stability_and_unstable_stops(self):
        self.assertEqual(POLICY['version'],'reported-pattern-v2')
        profiles={p:(3,3) for p in POLICY['profiles']}
        self.assertEqual(classify(70,10,profiles)[0],'high')
        self.assertEqual(classify(70,10,profiles,True),('unknown','unstable_stop_coordinates'))
        profiles['60m_120s']=(1,1)
        self.assertEqual(classify(70,10,profiles)[1],'matching_sensitive')
if __name__=='__main__':unittest.main()
