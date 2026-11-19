# AetherVision
## A Local-First Multimodal Intelligence Platform for Personal Media
AetherVision is an evolving personal media intelligence platform designed to transform heterogeneous media collections into searchable, contextual and interconnected knowledge.

The platform combines local media ingestion, metadata extraction, computer vision, multimodal AI inference and semantic retrieval to make personal media collections easier to explore, understand and connect.

Core principles
- Local-first: Personal media and derived data remain under user-controlled infrastructure.
- Privacy-oriented: AI processing is designed to operate locally without requiring external AI services.
- Resource-aware: Processing workloads account for available CPU, GPU, memory and storage resources.
- Incremental: Media collections can be indexed and enriched progressively.
- Extensible: Processing capabilities and inference models should evolve independently of the user-facing application.

## Architecture
AetherVision follows a local-first architecture with distinct responsibilities for media ingestion, metadata management, AI enrichment, retrieval and user interaction.

The current implementation is based on Python, Flask and SQLite, with local image processing and AI capabilities. The longer-term architectural direction is to evolve processing into independently managed, resource-aware inference components.

See Architecture Overview for the current system design, component responsibilities and architectural evolution.

Start page
<img width="1898" height="900" alt="Screenshot 2026-10-16 095923" src="https://github.com/user-attachments/assets/816c2be0-24f8-49e6-ab4e-7f5d5eb109c4" />
Map with image locations
<img width="1917" height="906" alt="Screenshot 2026-11-19 135608" src="https://github.com/user-attachments/assets/2855f8cb-b2bd-4bf4-8947-d51936372431" />


## Why ImageviewerAI?

Your photos already live in folders. ImageviewerAI indexes them in place
instead of forcing you to migrate your library into another platform.

With local AI, you can search for things such as:

- 🐕 Photos containing a dog
- 🚲 Photos containing bicycles
- ⚡ Electrical equipment
- 📄 Text appearing inside documents
- 🌲 Outdoor / nature scenes
- 📍 Photos with GPS information
- 🔎 Visually or semantically similar images

No cloud upload. No AI API key. No subscription.

## Features

### Photo library

- Index existing folders without moving your files
- Recursive directory scanning
- Detect new and modified images
- Detect moved files using content hashes
- EXIF metadata extraction
- GPS coordinates
- Automatic thumbnails
- Large-library browsing

### Local AI

- Automatic image tagging
- Semantic embeddings
- Image captions
- OCR
- Zero-shot classification
- Background processing
- GPU acceleration when available

### Search

Search across:

- filenames
- folders
- dates
- EXIF metadata
- AI-generated tags
- captions
- OCR text
- semantic embeddings

### Privacy

- Photos remain in their original locations
- AI processing runs locally
- No cloud photo service required
- No external AI API required
- No subscription


## Local AI pipeline

ImageviewerAI runs several computer-vision models locally to enrich your
photo library with searchable information.

| Model | Purpose |
|---|---|
| RAM++ | Automatic image tagging |
| SigLIP | Semantic embeddings and zero-shot classification |
| BLIP | Image captions |
| EasyOCR | Text extraction |

The resulting metadata is stored locally and can be searched without
sending your images to an external service.

```
Image
 │
 ├── EXIF ──────────────┐
 │                      │
 ├── Thumbnail          │
 │                      ▼
 └── AI processing ──► SQLite
       │                │
       ├── Tags         │
       ├── Captions     │
       ├── OCR          │
       └── Embeddings   │
                        │
                        ▼
                   Search/API
                        │
                        ▼
                  Web interface
```


## Installation
### Requirements
Python 3.10+
A machine with enough RAM/VRAM for the selected AI models
Your existing photo library
GPU acceleration is recommended for large libraries, although the exact requirements depend on which models you enable.

## Clone the repository
```
git clone https://github.com/berend21/ImageviewerAI.git
cd ImageviewerAI
```
## Install dependencies
```
pip install -r requirements.txt
```
## Start ImageviewerAI
```
python appim.py
```
Then open the local web interface in your browser.

The exact startup command and configuration may change as the project evolves. Check the repository configuration for the current recommended setup.

## Indexing a library
Point ImageviewerAI at an existing photo directory.

The indexer keeps track of files using metadata and content hashes, allowing it to avoid unnecessarily reprocessing unchanged files.

A typical workflow looks like:

Initial scan
    ↓
Read filesystem metadata
    ↓
Identify new/changed files
    ↓
Extract EXIF
    ↓
Generate thumbnails
    ↓
Run AI processing
    ↓
Store metadata + embeddings
    ↓
Searchable library

Subsequent scans can skip files that have not changed.

## Performance
Performance depends heavily on:

- number of images
- image resolution
- CPU
- GPU
- available RAM/VRAM
- enabled AI models
- filesystem/storage speed
- The expensive part of indexing is generally AI inference rather than database operations.

For large libraries, ImageviewerAI is therefore designed around incremental/background processing rather than requiring the entire library to be analyzed before it can be used.

Benchmarks for specific hardware are planned. Until reproducible benchmarks are available, performance claims should be treated as workload-dependent.

## Project status
ImageviewerAI is an actively developed personal project.

The core photo indexing and browsing functionality is usable, while the architecture and feature set are still evolving.

Areas of ongoing development include:

- search quality
- indexing performance
- AI model optimization
- library scalability
- UI improvements
- reliability and error handling
- automated testing
- deployment and security hardening

## ⚠️ Security

ImageviewerAI is intended primarily for trusted, local networks.

The application can expose:

- your images
- image metadata
- GPS coordinates
- filesystem information
- indexed photo operations

Do not expose the development server directly to the public internet.

If you need remote access, put ImageviewerAI behind appropriate
authentication and network controls.


Searching 'electrical'
<img width="1916" height="775" alt="Screenshot 2026-08-11 143527" src="https://github.com/user-attachments/assets/5b92c4a0-7b69-42c4-8540-bef38b5fb8a8" />
Searching 'bicycle'
<img width="1915" height="776" alt="Screenshot 2026-08-11 143602" src="https://github.com/user-attachments/assets/aa727074-43c3-4a72-a9da-210258202f9a" />
Searching 'tree'
<img width="1914" height="545" alt="Screenshot 2026-08-11 143817" src="https://github.com/user-attachments/assets/c88639f0-3955-4377-b54a-8ed38ea4e6b3" />

## Project status

🚧 **Early / active development**

ImageviewerAI is currently usable for local photo indexing, browsing and
AI-assisted search, but the project is still evolving.

### Working

- [x] Recursive image indexing
- [x] SQLite metadata database
- [x] Thumbnail generation
- [x] EXIF extraction
- [x] Content hashing
- [x] Web gallery
- [x] AI tagging
- [x] Semantic embeddings
- [x] Image captions
- [x] OCR
- [X] Map-based location browsing

### In progress

- [ ] Search quality improvements
- [ ] Faster AI processing
- [ ] Person recognition/search
- [ ] Album creation
- [ ] Link calendar events to photos
- [ ] Improved document/OCR search
- [ ] Automated tests
- [ ] Deployment hardening

## Known limitations

- OCR/full-text document search is still being improved.
- AI processing performance depends heavily on hardware.
- There are currently limited automated tests.

## Roadmap

- Person identification and named people
- Faster semantic search
- Improved GPU utilization
- Better deployment options

## Technology

| Component | Technology |
|---|---|
| Backend | Python / Flask |
| Database | SQLite |
| Full-text search | SQLite FTS5 |
| Image processing | Pillow |
| AI inference | PyTorch |
| Semantic search | SigLIP |
| Image tagging | RAM++ |
| Captioning | BLIP |
| OCR | EasyOCR |
| Frontend | HTML / CSS / JavaScript |
