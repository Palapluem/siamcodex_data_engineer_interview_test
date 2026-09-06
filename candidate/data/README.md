# Synthetic source data

These JSONL files are backing fixtures for the provided APIs, not an ingestion interface. You may inspect them to understand the source systems. Your submitted service must acquire business data through the gateway APIs, including during replay and recovery. Do not mount, copy, bake in, or read these files in your application.

All organizations, units, cases and contacts are fictional. Addresses use example.invalid. There are exactly 10,000 event envelopes across three source files; an event is not the same thing as a distinct case. The default seed is 73129. A different seed may be used for evaluation with the same documented semantics.

The fixture generator is infrastructure/generate_data.py. The seq field is an ordered source-local cursor. phase controls when the mock API releases an event and is not exposed by the API. manifest.json records file hashes and counts. Do not modify these files during the exercise.
