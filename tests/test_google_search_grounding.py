from unittest import TestCase

from hakimi.models.google_search import sources


class GoogleSourcesTests(TestCase):
    def test_only_associated_text_is_attached_to_each_link(self):
        response={'content':{'parts':[{'text':'unsourced extra claim'}]},'groundingMetadata':{
            'groundingChunks':[{'web':{'uri':'https://example.org/text','title':'Original article'}},
                               {'web':{'uri':'http://127.0.0.1/leak','title':'internal'}}],
            'groundingSupports':[{'segment':{'text':'The citation explicitly supports this sentence.'},'groundingChunkIndices':[0]}]}}
        result=sources(response)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['evidence_level'],'grounded_summary')
        self.assertNotIn('unsourced',result[0]['text'])
        self.assertIsNone(result[0]['published_at'])

    def test_model_text_alone_is_not_search_evidence(self):
        self.assertEqual(sources({'content':{'parts':[{'text':'I searched'}]}}),[])
