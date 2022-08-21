import flax
import flax.linen as nn
import jax
import jax.numpy as jnp
from PIL import Image
from transformers import CLIPTokenizer, FlaxCLIPTextModel

from stable_diffusion_jax.scheduling_pndm import PNDMScheduler


@flax.struct.dataclass
class InferenceState:
    text_encoder_params: flax.core.FrozenDict
    unet_params: flax.core.FrozenDict
    vae_params: flax.core.FrozenDict


class StableDiffusionPipeline:
    def __init__(self, vae, text_encoder, tokenizer, unet, scheduler):
        scheduler = scheduler.set_format("np")
        self.vae = vae
        self.text_encoder = text_encoder
        self.tokenizer = tokenizer
        self.unet = unet
        self.scheduler = scheduler

    def numpy_to_pil(images):
        """
        Convert a numpy image or a batch of images to a PIL image.
        """
        if images.ndim == 3:
            images = images[None, ...]
        images = (images * 255).round().astype("uint8")
        pil_images = [Image.fromarray(image) for image in images]

        return pil_images

    def sample(
        self,
        input_ids: jnp.ndarray,
        uncond_input_ids: jnp.ndarray,
        prng_seed: jax.random.PRNGKey,
        inference_state: InferenceState,
        num_inference_steps: int = 50,
        guidance_scale: float = 1.0,
    ):

        self.scheduler.set_timesteps(num_inference_steps)

        text_embeddings = self.text_encoder(input_ids, params=inference_state.text_encoder_params)[0]
        uncond_embeddings = self.text_encoder(uncond_input_ids, params=inference_state.text_encoder_params)[0]
        context = jnp.concatenate([uncond_embeddings, text_embeddings])

        latents = jax.random.normal(
            prng_seed,
            shape=(input_ids.shape[0], self.unet.in_channels, self.unet.sample_size, self.unet.sample_size),
            dtype=jnp.float32,
        )

        def loop_body(step, latents):
            t = jnp.array(self.scheduler.timesteps)[step]

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            latents_input = jnp.concatenate([latents] * 2)

            # predict the noise residual
            noise_pred = self.unet(
                latents_input, t, encoder_hidden_states=context, params=inference_state.unet_params
            )["sample"]
            # perform guidance
            noise_pred_uncond, noise_prediction_text = jnp.split(noise_pred, 2, axis=0)
            noise_pred = noise_pred_uncond + guidance_scale * (noise_prediction_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            latents = self.scheduler.step(noise_pred, t, latents)["prev_sample"]
            return latents

        latents = jax.lax.fori_loop(0, num_inference_steps, loop_body, latents)

        # TODO wait until vqvae is ready in FLAX and then correct that here
        # image = self.vqvae.decode(latents, params=inference_state.vae_params)
        # scale and decode the image latents with vae
        latents = 1 / 0.18215 * latents
        image = latents

        return image


# that's the official CLIP model and tokenizer Stable-diffusion uses
# see: https://github.com/CompVis/stable-diffusion/blob/ce05de28194041e030ccfc70c635fe3707cdfc30/configs/stable-diffusion/v1-inference.yaml#L70
# and https://github.com/CompVis/stable-diffusion/blob/ce05de28194041e030ccfc70c635fe3707cdfc30/ldm/modules/encoders/modules.py#L137
tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")
clip_model = FlaxCLIPTextModel.from_pretrained("openai/clip-vit-large-patch14")


class DummyUnet(nn.Module):
    in_channels = 3
    sample_size = 1

    @nn.compact
    def __call__(self, latents_input, t, encoder_hidden_states):
        return {"sample": latents_input + 1}


unet = DummyUnet()
scheduler = PNDMScheduler()


pipeline = FlaxLDMTextToImagePipeline(vqvae=None, clip=clip_model, tokenizer=tokenizer, unet=unet, scheduler=scheduler)

# now running the pipeline should work more or less which it doesn't at the moment @Nathan
key = jax.random.PRNGKey(0)

prompt = "A painting of a squirrel eating a burger"
images = pipeline([prompt], prng_seed=key, num_inference_steps=50, eta=0.3, guidance_scale=6)["sample"]
