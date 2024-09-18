# EDGE-Rec: Efficient and Data-Guided Edge Diffusion for Recommender System Graphs 
## *Authors: Edoardo Botta, Utkarsh Priyam, Hemit Shah*

This is the official repository of "[EDGE-Rec: Efficient and Data-Guided Edge Diffusion for Recommender System Graphs](https://genai-personalization.github.io/assets/papers/GenAIRecP2024/Priyam.pdf)", accepted to the Generative AI for Recommender Systems and Personalization workshop at ACM SIGKDD 2024 (annual Knowledge Discovery and Data Minining conference)

### Abstract

Most recommender systems research focuses on binary historical user-item interaction encodings to predict future interactions. User features, item features, and interaction strengths remain largely under-utilized in this space or only indirectly utilized, despite proving largely effective in large-scale production recommendation systems. We propose a new attention mechanism, loosely based on the principles of collaborative filtering, called Row-Column Separable Attention (**RCSA**) to take advantage of real-valued interaction weights as well as user and item features directly. Building on this mechanism, we additionally propose a novel Graph Diffusion Transformer (**GDiT**) architecture which is trained to iteratively denoise the weighted interaction matrix of the user-item interaction graph directly. The weighted interaction matrix is built from the bipartite structure of the user-item interaction graph and corresponding edge weights derived from user-item rating interactions. Inspired by the recent progress in text-conditioned image generation, our method directly produces user-item rating predictions on the same scale as the original ratings by conditioning the denoising process on user and item features with a principled approach.

### Replicability
Results can be replicated in a step-by-step fashion by running the [execute.ipynb](./execute.ipynb) notebook.

### Acknowledgments
The denoising diffusion model borrows from [denoising-diffusion-pytorch](https://github.com/lucidrains/denoising-diffusion-pytorch). 
