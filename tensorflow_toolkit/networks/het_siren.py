# **************************************************************************
# *
# * Authors:  David Herreros Calero (dherreros@cnb.csic.es)
# *
# * Unidad de  Bioinformatica of Centro Nacional de Biotecnologia , CSIC
# *
# * This program is free software; you can redistribute it and/or modify
# * it under the terms of the GNU General Public License as published by
# * the Free Software Foundation; either version 2 of the License, or
# * (at your option) any later version.
# *
# * This program is distributed in the hope that it will be useful,
# * but WITHOUT ANY WARRANTY; without even the implied warranty of
# * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# * GNU General Public License for more details.
# *
# * You should have received a copy of the GNU General Public License
# * along with this program; if not, write to the Free Software
# * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA
# * 02111-1307  USA
# *
# *  All comments concerning this program package may be sent to the
# *  e-mail address 'scipion@cnb.csic.es'
# *
# **************************************************************************


import tensorflow as tf
from tensorflow.keras import layers, models, Input, Model

from pathlib import Path
import numpy as np
from scipy.ndimage import gaussian_filter
from scipy import signal
from xmipp_metadata.image_handler import ImageHandler

from tensorflow_toolkit.utils import computeCTF, full_fft_pad, full_ifft_pad, create_blur_filters, \
    apply_blur_filters_to_batch
from tensorflow_toolkit.layers.siren import SIRENFirstLayerInitializer, SIRENInitializer, MetaDenseWrapper


##### Extra functions for HetSIREN network #####
def richardsonLucyDeconvolver(volume, iter=5):
    original_volume = volume.copy()
    volume = tf.constant(volume, dtype=tf.float32)
    original_volume = tf.constant(original_volume, dtype=tf.float32)

    std = np.pi * np.sqrt(volume.shape[1])
    gauss_1d = signal.windows.gaussian(volume.shape[1], std)
    kernel = np.einsum('i,j,k->ijk', gauss_1d, gauss_1d, gauss_1d)
    kernel = tf.constant(kernel, dtype=tf.float32)

    def applyKernelFourier(x):
        x = tf.cast(x, dtype=tf.complex64)
        ft_x = tf.signal.fftshift(tf.signal.fft3d(x))
        ft_x_real = tf.math.real(ft_x) * kernel
        ft_x_imag = tf.math.imag(ft_x) * kernel
        ft_x = tf.complex(ft_x_real, ft_x_imag)
        return tf.math.real(tf.signal.ifft3d(tf.signal.fftshift(ft_x)))

    for _ in range(iter):
        # Deconvolve image (update)
        conv_1 = applyKernelFourier(volume)
        conv_1_2 = conv_1 * conv_1
        epsilon = 0.1 * np.mean(conv_1_2[:])
        update = original_volume * conv_1 / (conv_1_2 + epsilon)
        update = applyKernelFourier(update)
        volume = volume * update

        volume = volume.numpy()
        thr = 1e-6
        volume = volume - (volume > thr) * thr + (volume < -thr) * thr - (volume == thr) * volume
        volume = tf.constant(volume, dtype=tf.float32)

    return volume.numpy()

def richardsonLucyBlindDeconvolver(volume, global_iter=5, iter=20):
    original_volume = volume.copy()
    volume = tf.constant(volume, dtype=tf.float32)
    original_volume = tf.constant(original_volume, dtype=tf.float32)

    # Create a gaussian kernel that will be used to blur the original acquisition
    std = 1.0
    gauss_1d = signal.windows.gaussian(volume.shape[1], std)
    kernel = np.einsum('i,j,k->ijk', gauss_1d, gauss_1d, gauss_1d)
    kernel = tf.constant(kernel, dtype=tf.float32)

    def applyKernelFourier(x, y):
        x = tf.cast(x, dtype=tf.complex64)
        y = tf.cast(y, dtype=tf.complex64)
        ft_x = tf.signal.fftshift(tf.signal.fft3d(x))
        ft_y = tf.abs(tf.signal.fftshift(tf.signal.fft3d(y)))
        ft_x_real = tf.math.real(ft_x) * ft_y
        ft_x_imag = tf.math.imag(ft_x) * ft_y
        ft_x = tf.complex(ft_x_real, ft_x_imag)
        return tf.math.real(tf.signal.ifft3d(tf.signal.fftshift(ft_x)))

    for _ in range(global_iter):
        for _ in range(iter):
            # Deconvolve image (update)
            conv_1 = applyKernelFourier(volume, kernel)
            conv_1_2 = conv_1 * conv_1
            epsilon = 1e-6 * tf.reduce_mean(conv_1_2)
            update = original_volume * conv_1 / (conv_1_2 + epsilon)
            update = applyKernelFourier(update, tf.reverse(kernel, axis=[0, 1, 2]))
            volume = volume * update

            # volume = volume.numpy()
            # thr = 1e-6
            # volume = volume - (volume > thr) * thr + (volume < -thr) * thr - (volume == thr) * volume
            # volume = tf.constant(volume, dtype=tf.float32)

        for _ in range(iter):
            # Update kernel
            conv_1 = applyKernelFourier(kernel, volume)
            conv_1_2 = conv_1 * conv_1
            epsilon = 1e-6 * tf.reduce_mean(conv_1_2)
            update = original_volume * conv_1 / (conv_1_2 + epsilon)
            update = applyKernelFourier(update, tf.reverse(volume, axis=[0, 1, 2]))
            kernel = kernel * update

            # kernel = kernel.numpy()
            # thr = 1e-6
            # kernel = kernel - (kernel > thr) * thr + (kernel < -thr) * thr - (kernel == thr) * kernel
            # kernel = tf.constant(kernel, dtype=tf.float32)

    return volume

def deconvolveTV(volume, iterations, regularization_weight, lr=0.01):
    original = tf.Variable(volume, dtype=tf.float32)

    # Create a gaussian kernel that will be used to blur the original acquisition
    std = 1.0
    gauss_1d = signal.windows.gaussian(volume.shape[1], std)
    psf = np.einsum('i,j,k->ijk', gauss_1d, gauss_1d, gauss_1d)
    psf = tf.constant(psf, dtype=tf.float32)

    def applyKernelFourier(x, y):
        x = tf.cast(x, dtype=tf.complex64)
        y = tf.cast(y, dtype=tf.complex64)
        ft_x = tf.signal.fftshift(tf.signal.fft3d(x))
        ft_y = tf.abs(tf.signal.fftshift(tf.signal.fft3d(y)))
        ft_x_real = tf.math.real(ft_x) * ft_y
        ft_x_imag = tf.math.imag(ft_x) * ft_y
        ft_x = tf.complex(ft_x_real, ft_x_imag)
        return tf.math.real(tf.signal.ifft3d(tf.signal.fftshift(ft_x)))

    for i in range(iterations):
        with tf.GradientTape() as tape:
            # Convolve with PSF
            # convolved = tf.nn.conv2d(tf.expand_dims(original, axis=0), tf.expand_dims(psf, axis=0), strides=[1, 1, 1, 1], padding='SAME')
            # convolved = tf.squeeze(convolved)
            convolved = applyKernelFourier(volume, psf)

            # Calculate the loss (data fidelity term + TV regularization)
            loss = tf.reduce_mean(tf.square(convolved - volume)) + regularization_weight * tf.reduce_sum(tf.image.total_variation(original))

        # Perform a gradient descent step
        grads = tape.gradient(loss, [original])
        # grads = tf.gradients(loss, [original])
        original.assign_sub(lr * grads[0])

    return original.numpy()

def tv_deconvolution_bregman(volume, iterations, regularization_weight, lr=0.01):
    deconvolved = tf.Variable(volume, dtype=tf.float32)
    bregman = tf.Variable(tf.zeros_like(volume), dtype=tf.float32)

    # Create a gaussian kernel that will be used to blur the original acquisition
    std = 1.0
    gauss_1d = signal.windows.gaussian(volume.shape[1], std)
    psf = np.einsum('i,j,k->ijk', gauss_1d, gauss_1d, gauss_1d)
    psf_tf = tf.constant(psf, dtype=tf.float32)
    # psf_mirror = tf.reverse(tf.reverse(psf_tf, axis=[0]), axis=[1])

    def applyKernelFourier(x, y):
        x = tf.cast(x, dtype=tf.complex64)
        y = tf.cast(y, dtype=tf.complex64)
        ft_x = tf.signal.fftshift(tf.signal.fft3d(x))
        ft_y = tf.abs(tf.signal.fftshift(tf.signal.fft3d(y)))
        ft_x_real = tf.math.real(ft_x) * ft_y
        ft_x_imag = tf.math.imag(ft_x) * ft_y
        ft_x = tf.complex(ft_x_real, ft_x_imag)
        return tf.math.real(tf.signal.ifft3d(tf.signal.fftshift(ft_x)))

    for i in range(iterations):
        with tf.GradientTape() as tape:
            # Convolve with PSF
            # convolved = tf.nn.conv2d(tf.expand_dims(original, axis=0), tf.expand_dims(psf, axis=0), strides=[1, 1, 1, 1], padding='SAME')
            # convolved = tf.squeeze(convolved)
            convolved = applyKernelFourier(volume, psf)

            # Calculate the loss (data fidelity term + TV regularization)
            loss = tf.reduce_mean(tf.square(convolved - volume)) + regularization_weight * tf.reduce_sum(tf.image.total_variation(deconvolved - bregman))

        # Perform a gradient descent step
        grads = tape.gradient(loss, [deconvolved])
        # grads = tf.gradients(loss, [deconvolved])
        deconvolved.assign_sub(lr * grads[0])

        # Bregman Update
        bregman.assign(bregman + deconvolved - tv_minimization_step(deconvolved, lr))

    return deconvolved.numpy()

def tv_minimization_step(image, lr):
    # Implement the TV minimization step
    # This is a placeholder function, in practice, you'll need a proper implementation
    return image - lr * tf.image.total_variation(image)


### Image smoothness with TV ###
def total_variation_loss(volume, diff1, diff2, diff3):
    """
    Computes the Total Variation Loss.
    Encourages spatial smoothness in the image output.

    Parameters:
    volume (Tensor): The image tensor of shape (batch_size, depth, height, width)
    diff1 (Tensor): Voxel value differences of shape (batch_size, depth - 1, height, width)
    diff2 (Tensor): Voxel value differences of shape (batch_size, depth, height - 1, width)
    diff3 (Tensor): Voxel value differences of shape (batch_size, depth, height, width - 1)

    Returns:
    Tensor: The total variation loss.
    """

    # Sum for both directions.
    sum_axis = [1, 2, 3]
    loss = tf.reduce_sum(tf.abs(diff1), axis=sum_axis) + \
           tf.reduce_sum(tf.abs(diff2), axis=sum_axis) + \
           tf.reduce_sum(tf.abs(diff3), axis=sum_axis)

    # Normalize by the volume size
    num_pixels = tf.cast(tf.reduce_prod(volume.shape[1:]), tf.float32)
    loss /= num_pixels

    return loss


def mse_smoothness_loss(volume, diff1, diff2, diff3):
    """
    Computes an MSE-based smoothness loss.
    This loss penalizes large intensity differences between adjacent pixels.

    Parameters:
    volume (Tensor): The image tensor of shape (batch_size, depth, height, width)
    diff1 (Tensor): Voxel value differences of shape (batch_size, depth - 1, height, width)
    diff2 (Tensor): Voxel value differences of shape (batch_size, depth, height - 1, width)
    diff3 (Tensor): Voxel value differences of shape (batch_size, depth, height, width - 1)

    Returns:
    Tensor: The MSE-based smoothness loss.
    """

    # Square differences
    diff1 = tf.square(diff1)
    diff2 = tf.square(diff2)
    diff3 = tf.square(diff3)

    # Sum the squared differences
    sum_axis = [1, 2, 3]
    loss = tf.reduce_sum(diff1, axis=sum_axis) + tf.reduce_sum(diff2, axis=sum_axis) + tf.reduce_sum(diff3, axis=sum_axis)

    # Normalize by the number of pixel pairs
    num_pixel_pairs = tf.cast(2 * tf.reduce_prod(volume.shape[1:3]) - volume.shape[1] - volume.shape[2], tf.float32)
    loss /= num_pixel_pairs

    return loss

def densitySmoothnessVolume(xsize, indices, values):
    B = tf.shape(values)[0]

    grid = tf.zeros((B, xsize, xsize, xsize), dtype=tf.float32)
    indices = tf.tile(tf.cast(indices[None, ...], dtype=tf.int32), (B, 1, 1))

    # Scatter in volumes
    fn = lambda inp: tf.tensor_scatter_nd_add(inp[0], inp[1], inp[2])
    grid = tf.map_fn(fn, [grid, indices, values], fn_output_signature=tf.float32)

    # Calculate the differences of neighboring pixel-values.
    # The total variation loss is the sum of absolute differences of neighboring pixels
    # in both dimensions.
    pixel_diff1 = grid[:, 1:, :, :] - grid[:, :-1, :, :]
    pixel_diff2 = grid[:, :, 1:, :] - grid[:, :, :-1, :]
    pixel_diff3 = grid[:, :, :, 1:] - grid[:, :, :, :-1]

    # Compute total variation and density MSE losses
    return (total_variation_loss(grid, pixel_diff1, pixel_diff2, pixel_diff3),
            mse_smoothness_loss(grid, pixel_diff1, pixel_diff2, pixel_diff3))


def filterVol(volume):
    size = volume.shape[1]
    volume = tf.constant(volume, dtype=tf.float32)

    b_spline_1d = np.asarray([0.0, 0.5, 1.0, 0.5, 0.0])

    pad_before = (size - len(b_spline_1d)) // 2
    pad_after = size - pad_before - len(b_spline_1d)

    kernel = np.einsum('i,j,k->ijk', b_spline_1d, b_spline_1d, b_spline_1d)
    kernel = np.pad(kernel, (pad_before, pad_after), 'constant', constant_values=(0.0,))
    kernel = tf.constant(kernel, dtype=tf.complex64)
    ft_kernel = tf.abs(tf.signal.fftshift(tf.signal.fft3d(kernel)))

    # Create a gaussian kernel that will be used to blur the original acquisition
    # std = 2.0
    # gauss_1d = signal.windows.gaussian(volume.shape[1], std)
    # kernel = np.einsum('i,j,k->ijk', gauss_1d, gauss_1d, gauss_1d)
    # kernel = tf.constant(kernel, dtype=tf.complex64)
    # ft_kernel = tf.abs(tf.signal.fftshift(tf.signal.fft3d(kernel)))

    def applyKernelFourier(x):
        x = tf.cast(x, dtype=tf.complex64)
        ft_x = tf.signal.fftshift(tf.signal.fft3d(x))
        ft_x_real = tf.math.real(ft_x) * ft_kernel
        ft_x_imag = tf.math.imag(ft_x) * ft_kernel
        ft_x = tf.complex(ft_x_real, ft_x_imag)
        return tf.math.real(tf.signal.ifft3d(tf.signal.fftshift(ft_x)))

    volume = applyKernelFourier(volume).numpy()
    thr = 1e-6
    volume = volume - (volume > thr) * thr + (volume < -thr) * thr - (volume == thr) * volume

    return volume

def resizeImageFourier(images, out_size, pad_factor=1):
    # Sizes
    xsize = tf.shape(images)[1]
    pad_size = pad_factor * xsize
    pad_out_size = pad_factor * out_size

    # Fourier transform
    ft_images = full_fft_pad(images, pad_size, pad_size)

    # Normalization constant
    norm = tf.cast(pad_out_size, dtype=tf.float32) / tf.cast(pad_size, dtype=tf.float32)

    # Resizing
    ft_images = tf.image.resize_with_crop_or_pad(ft_images[..., None], pad_out_size, pad_out_size)[..., 0]

    # Inverse transform
    images = full_ifft_pad(ft_images, out_size, out_size)
    images *= norm * norm

    return images

def normalize_to_other_volumes(batch1, batch2):
    """
    Normalize volumes in batch2 to have the same mean and std as the corresponding volumes in batch1.

    Parameters:
    batch1, batch2: numpy arrays of shape (B, W, H, D) representing batches of volumes.

    Returns:
    normalized_batch2: numpy array of normalized images.
    """
    # Calculate mean and std for each image in batch1
    means1 = batch1.mean(axis=(1, 2, 3), keepdims=True)
    stds1 = batch1.std(axis=(1, 2, 3), keepdims=True)

    # Calculate mean and std for each image in batch2
    means2 = batch2.mean(axis=(1, 2, 3), keepdims=True)
    stds2 = batch2.std(axis=(1, 2, 3), keepdims=True)

    # Normalize batch2 to have the same mean and std as batch1
    normalized_batch2 = ((batch2 - means2) / stds2) * stds1 + means1

    return normalized_batch2


def match_histograms(source, reference):
    """
    Adjust the pixel values of a N-D source volume to match the histogram of a reference volume.

    Parameters:
    - source: ndarray
      Input volume. Can be of shape (B, W, H, D).
    - reference: ndarray
      Reference volume. Must have the same shape as the source.

    Returns:
    - matched: ndarray
      The source volume after histogram matching.
    """
    matched = np.zeros_like(source)

    for b in range(source.shape[0]):
        # Flatten the volumes
        s_values = source[b].ravel()
        r_values = reference[b].ravel()

        # Get unique values and their corresponding indices for both source and reference
        s_values_unique, s_inverse = np.unique(s_values, return_inverse=True)
        r_values_unique, r_counts = np.unique(r_values, return_counts=True)

        # Calculate the CDF for the source and reference
        s_quantiles = np.cumsum(np.bincount(s_inverse, minlength=s_values_unique.size))
        s_quantiles = s_quantiles / s_quantiles[-1]
        r_quantiles = np.cumsum(r_counts)
        r_quantiles = r_quantiles / r_quantiles[-1]

        # Interpolate
        interp_r_values = np.interp(s_quantiles, r_quantiles, r_values_unique)

        # Map the source pixels to the reference pixels
        matched[b] = interp_r_values[s_inverse].reshape(source[b].shape)

    return matched


class Encoder(Model):
    def __init__(self, latent_dim, input_dim, architecture="convnn", refPose=True,
                 mode="spa"):
        super(Encoder, self).__init__()
        filters = create_blur_filters(5, 5, 15)

        images = Input(shape=(input_dim, input_dim, 1))
        subtomo_pe = Input(shape=(100,))

        if architecture == "convnn":
            x = tf.keras.layers.Flatten()(images)
            x = tf.keras.layers.Dense(64 * 64)(x)
            x = tf.keras.layers.Reshape((64, 64, 1))(x)

            x = tf.keras.layers.Conv2D(4, 5, activation="relu", strides=(2, 2), padding="same")(x)
            b1_out = tf.keras.layers.Conv2D(8, 5, activation="relu", strides=(2, 2), padding="same")(x)

            b2_x = tf.keras.layers.Conv2D(8, 1, activation="relu", strides=(1, 1), padding="same")(b1_out)
            b2_x = tf.keras.layers.Conv2D(8, 1, activation="linear", strides=(1, 1), padding="same")(b2_x)
            b2_add = layers.Add()([b1_out, b2_x])
            b2_add = layers.ReLU()(b2_add)

            for _ in range(1):
                b2_x = tf.keras.layers.Conv2D(8, 1, activation="linear", strides=(1, 1), padding="same")(b2_add)
                b2_add = layers.Add()([b2_add, b2_x])
                b2_add = layers.ReLU()(b2_add)

            b2_out = tf.keras.layers.Conv2D(16, 3, activation="relu", strides=(2, 2), padding="same")(b2_add)

            b3_x = tf.keras.layers.Conv2D(16, 1, activation="relu", strides=(1, 1), padding="same")(b2_out)
            b3_x = tf.keras.layers.Conv2D(16, 1, activation="linear", strides=(1, 1), padding="same")(b3_x)
            b3_add = layers.Add()([b2_out, b3_x])
            b3_add = layers.ReLU()(b3_add)

            for _ in range(1):
                b3_x = tf.keras.layers.Conv2D(16, 1, activation="linear", strides=(1, 1), padding="same")(b3_add)
                b3_add = layers.Add()([b3_add, b3_x])
                b3_add = layers.ReLU()(b3_add)

            b3_out = tf.keras.layers.Conv2D(16, 3, activation="relu", strides=(2, 2), padding="same")(b3_add)
            x = tf.keras.layers.Flatten()(b3_out)

            x = layers.Flatten()(x)
            for _ in range(4):
                x = layers.Dense(256, activation='relu')(x)

        elif architecture == "deepconv":
            x = resizeImageFourier(images, 64)
            x = apply_blur_filters_to_batch(x, filters)

            x = tf.keras.layers.Conv2D(64, 5, activation="relu", strides=(2, 2), padding="same")(x)
            b1_out = tf.keras.layers.Conv2D(128, 5, activation="relu", strides=(2, 2), padding="same")(x)

            b2_x = tf.keras.layers.Conv2D(128, 1, activation="relu", strides=(1, 1), padding="same")(b1_out)
            b2_x = tf.keras.layers.Conv2D(128, 1, activation="linear", strides=(1, 1), padding="same")(b2_x)
            b2_add = layers.Add()([b1_out, b2_x])
            b2_add = layers.ReLU()(b2_add)

            for _ in range(12):
                b2_x = tf.keras.layers.Conv2D(128, 1, activation="linear", strides=(1, 1), padding="same")(b2_add)
                b2_add = layers.Add()([b2_add, b2_x])
                b2_add = layers.ReLU()(b2_add)

            b2_out = tf.keras.layers.Conv2D(256, 3, activation="relu", strides=(2, 2), padding="same")(b2_add)

            b3_x = tf.keras.layers.Conv2D(256, 1, activation="relu", strides=(1, 1), padding="same")(b2_out)
            b3_x = tf.keras.layers.Conv2D(256, 1, activation="linear", strides=(1, 1), padding="same")(b3_x)
            b3_add = layers.Add()([b2_out, b3_x])
            b3_add = layers.ReLU()(b3_add)

            for _ in range(12):
                b3_x = tf.keras.layers.Conv2D(256, 1, activation="linear", strides=(1, 1), padding="same")(b3_add)
                b3_add = layers.Add()([b3_add, b3_x])
                b3_add = layers.ReLU()(b3_add)

            b3_out = tf.keras.layers.Conv2D(512, 3, activation="relu", strides=(2, 2), padding="same")(b3_add)
            x = tf.keras.layers.Flatten()(b3_out)

            x = layers.Flatten()(x)
            x = layers.Dense(512, activation='relu')(x)
            for _ in range(4):
                aux = layers.Dense(512, activation='relu')(x)
                x = layers.Add()([x, aux])

        elif architecture == "mlpnn":
            x = layers.Flatten()(images)
            x = layers.Dense(1024, activation='relu')(x)
            for _ in range(2):
                x = layers.Dense(1024, activation='relu')(x)

        if mode == "spa":
            latent = layers.Dense(256, activation="relu")(x)
        elif mode == "tomo":
            latent = layers.Dense(1024, activation="relu")(subtomo_pe)
            for _ in range(2):  # TODO: Is it better to use 12 hidden layers as in Zernike3Deep?
                latent = layers.Dense(1024, activation="relu")(latent)
        for _ in range(2):
            latent = layers.Dense(256, activation="relu")(latent)
        latent = layers.Dense(latent_dim, activation="linear")(latent)  # Tanh [-1,1] as needed by SIREN?

        rows = layers.Dense(256, activation="relu", trainable=refPose)(x)
        for _ in range(2):
            rows = layers.Dense(256, activation="relu", trainable=refPose)(rows)
        rows = layers.Dense(3, activation="linear", trainable=refPose)(rows)

        shifts = layers.Dense(256, activation="relu", trainable=refPose)(x)
        for _ in range(2):
            shifts = layers.Dense(256, activation="relu", trainable=refPose)(shifts)
        shifts = layers.Dense(2, activation="linear", trainable=refPose)(shifts)

        if mode == "spa":
            self.encoder = Model(images, [rows, shifts, latent], name="encoder")
        elif mode == "tomo":
            self.encoder = Model([images, subtomo_pe], [rows, shifts, latent], name="encoder")
            self.encoder_latent = Model(subtomo_pe, latent, name="encode_latent")

    def call(self, x):
        encoded = self.encoder(x)
        return encoded


class Decoder(Model):
    def __init__(self, latent_dim, generator, CTF="apply"):
        super(Decoder, self).__init__()
        self.generator = generator
        self.CTF = CTF
        w0_first = 30.0 if generator.step == 1 else 30.0

        rows = Input(shape=(3,))
        shifts = Input(shape=(2,))
        latent = Input(shape=(latent_dim,))

        coords = layers.Lambda(self.generator.getRotatedGrid)(rows)

        # Volume decoder
        count = 0
        delta_het = MetaDenseWrapper(latent_dim, latent_dim, latent_dim, w0=w0_first,
                                     meta_kernel_initializer=SIRENFirstLayerInitializer(scale=6.0),
                                     name=f"het_{count}")(latent)  # activation=Sine(w0=1.0)
        for _ in range(3):
            count += 1
            aux = MetaDenseWrapper(latent_dim, latent_dim, latent_dim, w0=1.0,
                                   meta_kernel_initializer=SIRENInitializer(),
                                   name=f"het_{count}")(delta_het)
            delta_het = layers.Add()([delta_het, aux])
        count += 1
        delta_het = layers.Dense(self.generator.total_voxels, activation='linear',
                                 name=f"het_{count}", kernel_initializer=self.generator.weight_initializer)(delta_het)

        # Scatter image and bypass gradient
        decoded_het = layers.Lambda(self.generator.scatterImgByPass)([coords, shifts, delta_het])

        # Gaussian filter image
        decoded_het = layers.Lambda(self.generator.gaussianFilterImage)(decoded_het)

        # Soft threshold image
        decoded_het = layers.Lambda(self.generator.softThresholdImage)(decoded_het)

        if self.CTF == "apply":
            # CTF filter image
            decoded_het = layers.Lambda(self.generator.ctfFilterImage)(decoded_het)

        self.decode_het = Model(latent, delta_het, name="decoder_het")
        self.decoder = Model([rows, shifts, latent], decoded_het, name="decoder")

    def eval_volume_het(self, x_het, filter=True, only_pos=False):
        batch_size = x_het.shape[0]

        values = self.generator.values[None, :] + self.decode_het(x_het)

        # Coords indices
        o_z, o_y, o_x = (self.generator.indices[:, 0].astype(int), self.generator.indices[:, 1].astype(int),
                         self.generator.indices[:, 2].astype(int))

        # Get numpy volumes
        values = values.numpy()
        volume_grids = np.zeros((batch_size, self.generator.xsize, self.generator.xsize, self.generator.xsize), dtype=np.float32)
        for idx in range(batch_size):
            volume_grids[idx, o_z, o_y, o_x] = values[idx]
            if filter:
                volume_grids[idx] = filterVol(volume_grids[idx])

            # Only for deconvolvers
            if not only_pos:
                neg_part = volume_grids[idx] * (volume_grids[idx] < 0.0)
            volume_grids[idx] = volume_grids[idx] * (volume_grids[idx] >= 0.0)

            # Deconvolvers
            # volume_grids[idx] = richardsonLucyDeconvolver(volume_grids[idx])
            # volume_grids[idx] = richardsonLucyBlindDeconvolver(volume_grids[idx], global_iter=5, iter=5)
            # volume_grids[idx] = deconvolveTV(volume_grids[idx], iterations=50, regularization_weight=0.001, lr=0.01)
            # volume_grids[idx] = tv_deconvolution_bregman(volume_grids[idx], iterations=50,
            #                                              regularization_weight=0.1, lr=0.01)

            if not only_pos:
                volume_grids[idx] += neg_part

        return volume_grids.astype(np.float32)

    def call(self, x):
        decoded = self.decoder(x)
        return decoded


class AutoEncoder(Model):
    def __init__(self, generator, het_dim=10, architecture="convnn", CTF="wiener", refPose=True,
                 l1_lambda=0.5, tv_lambda=0.5, mse_lambda=0.5, mode=None, train_size=None, only_pos=True,
                 multires_levels=None, **kwargs):
        super(AutoEncoder, self).__init__(**kwargs)
        self.CTF = CTF if generator.applyCTF == 1 else None
        self.mode = generator.mode if mode is None else mode
        self.xsize = generator.metadata.getMetaDataImage(0).shape[1] if generator.metadata.binaries else generator.xsize
        self.encoder = Encoder(het_dim, self.xsize, architecture=architecture,
                               refPose=refPose, mode=self.mode)
        self.decoder = Decoder(het_dim, generator, CTF=CTF)
        self.refPose = 1.0 if refPose else 0.0
        self.l1_lambda = l1_lambda
        self.tv_lambda = tv_lambda
        self.mse_lambda = mse_lambda
        self.het_dim = het_dim
        self.only_pos = only_pos
        self.train_size = train_size if train_size is not None else self.xsize
        self.multires_levels = multires_levels
        if multires_levels is None:
            self.filters = None
        else:
            self.filters = create_blur_filters(multires_levels, 10, 30)
        self.total_loss_tracker = tf.keras.metrics.Mean(name="total_loss")
        self.test_loss_tracker = tf.keras.metrics.Mean(name="test_loss")
        self.loss_het_tracker = tf.keras.metrics.Mean(name="rec_het")

    @property
    def metrics(self):
        return [
            self.total_loss_tracker,
            self.test_loss_tracker,
            self.loss_het_tracker,
        ]

    def train_step(self, data):
        inputs = data[0]

        if self.mode == "spa":
            indexes = data[1]
            images = inputs
        elif self.mode == "tomo":
            indexes = data[1][0]
            images = inputs[0]

        self.decoder.generator.indexes = indexes
        self.decoder.generator.current_images = images

        # Update batch_size (in case it is incomplete)
        batch_size_scope = tf.shape(images)[0]

        # Precompute batch aligments
        self.decoder.generator.rot_batch = tf.gather(self.decoder.generator.angle_rot, indexes, axis=0)
        self.decoder.generator.tilt_batch = tf.gather(self.decoder.generator.angle_tilt, indexes, axis=0)
        self.decoder.generator.psi_batch = tf.gather(self.decoder.generator.angle_psi, indexes, axis=0)

        # Precompute batch shifts
        self.decoder.generator.shifts_batch = [tf.gather(self.decoder.generator.shift_x, indexes, axis=0),
                                               tf.gather(self.decoder.generator.shift_y, indexes, axis=0)]

        # Precompute batch CTFs
        defocusU_batch = tf.gather(self.decoder.generator.defocusU, indexes, axis=0)
        defocusV_batch = tf.gather(self.decoder.generator.defocusV, indexes, axis=0)
        defocusAngle_batch = tf.gather(self.decoder.generator.defocusAngle, indexes, axis=0)
        cs_batch = tf.gather(self.decoder.generator.cs, indexes, axis=0)
        kv_batch = self.decoder.generator.kv
        ctf = computeCTF(defocusU_batch, defocusV_batch, defocusAngle_batch, cs_batch, kv_batch,
                         self.decoder.generator.sr, self.decoder.generator.pad_factor,
                         [self.decoder.generator.xsize, int(0.5 * self.decoder.generator.xsize + 1)],
                         batch_size_scope, self.decoder.generator.applyCTF)
        self.decoder.generator.ctf = ctf

        # Wiener filter
        if self.CTF == "wiener":
            images = self.decoder.generator.wiener2DFilter(images)
            if self.mode == "spa":
                inputs = images
            elif self.mode == "tomo":
                inputs[0] = images

        with tf.GradientTape() as tape:
            rows, shifts, het = self.encoder(inputs)
            decoded_het = self.decoder([self.refPose * rows, self.refPose * shifts, het])

            # L1 penalization delta_het
            delta_het = self.decoder.decode_het(het) + self.decoder.generator.values[None, :]
            l1_loss_het = tf.reduce_mean(tf.reduce_sum(tf.abs(delta_het), axis=1))
            l1_loss_het = self.l1_lambda * l1_loss_het / self.decoder.generator.total_voxels

            # Total variation and MSE losses
            tv_loss, d_mse_loss = densitySmoothnessVolume(self.decoder.generator.xsize,
                                                          self.decoder.generator.indices, delta_het)
            tv_loss *= self.tv_lambda
            d_mse_loss *= self.mse_lambda

            # Negative loss
            mask = tf.less(delta_het, 0.0)
            delta_neg = tf.boolean_mask(delta_het, mask)
            delta_neg_size = tf.cast(tf.shape(delta_neg)[-1], dtype=tf.float32)
            delta_neg = tf.reduce_mean(tf.abs(delta_neg))
            neg_loss_het = self.l1_lambda * delta_neg / delta_neg_size

            # # Positive loss
            # mask = tf.greater(delta_het, 0.0)
            # delta_pos = tf.boolean_mask(delta_het, mask)
            # delta_pos_size = tf.cast(tf.shape(delta_pos)[-1], dtype=tf.float32)
            # delta_pos = tf.reduce_mean(tf.abs(delta_pos))
            # pos_loss_het = self.l1_lambda * delta_pos / delta_pos_size

            # Reconstruction mask for projections (Decoder size)
            mask_imgs = self.decoder.generator.resizeImageFourier(self.decoder.generator.mask_imgs,
                                                                  self.decoder.generator.xsize)
            mask_imgs = tf.abs(mask_imgs)
            mask_imgs = tf.math.divide_no_nan(mask_imgs, mask_imgs)

            # Reconstruction loss for original size images
            images_masked = mask_imgs * self.decoder.generator.resizeImageFourier(images, self.decoder.generator.xsize)
            loss_het_ori = self.decoder.generator.cost_function(images_masked, decoded_het)

            # Reconstruction mask for projections (Train size)
            mask_imgs = self.decoder.generator.resizeImageFourier(self.decoder.generator.mask_imgs, self.train_size)
            mask_imgs = tf.abs(mask_imgs)
            mask_imgs = tf.math.divide_no_nan(mask_imgs, mask_imgs)

            # Reconstruction loss for downscaled images
            images_masked = mask_imgs * self.decoder.generator.resizeImageFourier(images, self.train_size)
            decoded_het_scl = self.decoder.generator.resizeImageFourier(decoded_het, self.train_size)
            loss_het_scl = self.decoder.generator.cost_function(images_masked, decoded_het_scl)

            # MR loss
            if self.filters is not None:
                filt_images = apply_blur_filters_to_batch(images, self.filters)
                filt_decoded = apply_blur_filters_to_batch(decoded_het, self.filters)
                for idx in range(self.multires_levels):
                    loss_het_ori += self.decoder.generator.cost_function(filt_images[..., idx][..., None],
                                                                         filt_decoded[..., idx][..., None])
                loss_het_ori = loss_het_ori / (float(self.multires_levels) + 1)

            # Final losses
            rec_loss = loss_het_ori + loss_het_scl
            reg_loss = l1_loss_het + neg_loss_het + tv_loss + d_mse_loss

            total_loss = 0.5 * rec_loss + 0.5 * reg_loss

        grads = tape.gradient(total_loss, self.trainable_weights)
        self.optimizer.apply_gradients(zip(grads, self.trainable_weights))

        self.total_loss_tracker.update_state(total_loss)
        self.loss_het_tracker.update_state(rec_loss)
        return {
            "loss": self.total_loss_tracker.result(),
            "rec_loss": self.loss_het_tracker.result(),
        }

    def test_step(self, data):
        inputs = data[0]

        if self.mode == "spa":
            indexes = data[1]
            images = inputs
        elif self.mode == "tomo":
            indexes = data[1][0]
            images = inputs[0]

        self.decoder.generator.indexes = indexes
        self.decoder.generator.current_images = images

            # Update batch_size (in case it is incomplete)
        batch_size_scope = tf.shape(images)[0]

        # Precompute batch aligments
        self.decoder.generator.rot_batch = tf.gather(self.decoder.generator.angle_rot, indexes, axis=0)
        self.decoder.generator.tilt_batch = tf.gather(self.decoder.generator.angle_tilt, indexes, axis=0)
        self.decoder.generator.psi_batch = tf.gather(self.decoder.generator.angle_psi, indexes, axis=0)

        # Precompute batch shifts
        self.decoder.generator.shifts_batch = [tf.gather(self.decoder.generator.shift_x, indexes, axis=0),
                                               tf.gather(self.decoder.generator.shift_y, indexes, axis=0)]

        # Precompute batch CTFs
        defocusU_batch = tf.gather(self.decoder.generator.defocusU, indexes, axis=0)
        defocusV_batch = tf.gather(self.decoder.generator.defocusV, indexes, axis=0)
        defocusAngle_batch = tf.gather(self.decoder.generator.defocusAngle, indexes, axis=0)
        cs_batch = tf.gather(self.decoder.generator.cs, indexes, axis=0)
        kv_batch = self.decoder.generator.kv
        ctf = computeCTF(defocusU_batch, defocusV_batch, defocusAngle_batch, cs_batch, kv_batch,
                         self.decoder.generator.sr, self.decoder.generator.pad_factor,
                         [self.decoder.generator.xsize, int(0.5 * self.decoder.generator.xsize + 1)],
                         batch_size_scope, self.decoder.generator.applyCTF)
        self.decoder.generator.ctf = ctf

        # Wiener filter
        if self.CTF == "wiener":
            images = self.decoder.generator.wiener2DFilter(images)
            if self.mode == "spa":
                inputs = images
            elif self.mode == "tomo":
                inputs[0] = images

        rows, shifts, het = self.encoder(inputs)
        decoded_het = self.decoder([self.refPose * rows, self.refPose * shifts, het])

        # L1 penalization delta_het
        delta_het = self.decoder.decode_het(het) + self.decoder.generator.values[None, :]
        l1_loss_het = tf.reduce_mean(tf.reduce_sum(tf.abs(delta_het), axis=1))
        l1_loss_het = self.l1_lambda * l1_loss_het / self.decoder.generator.total_voxels

        # Total variation and MSE losses
        tv_loss, d_mse_loss = densitySmoothnessVolume(self.decoder.generator.xsize,
                                                      self.decoder.generator.indices, delta_het)
        tv_loss *= self.tv_lambda
        d_mse_loss *= self.mse_lambda

        # Negative loss
        if self.only_pos:
            mask = tf.less(delta_het, 0.0)
            delta_neg = tf.boolean_mask(delta_het, mask)
            delta_neg_size = tf.cast(tf.shape(delta_neg)[-1], dtype=tf.float32)
            delta_neg = tf.reduce_mean(tf.abs(delta_neg))
            neg_loss_het = self.l1_lambda * delta_neg / delta_neg_size
        else:
            neg_loss_het = 0.0

        # Reconstruction mask for projections (Decoder size)
        mask_imgs = self.decoder.generator.resizeImageFourier(self.decoder.generator.mask_imgs,
                                                              self.decoder.generator.xsize)
        mask_imgs = tf.abs(mask_imgs)
        mask_imgs = tf.math.divide_no_nan(mask_imgs, mask_imgs)

        # Reconstruction loss for original size images
        images_masked = mask_imgs * self.decoder.generator.resizeImageFourier(images, self.decoder.generator.xsize)
        loss_het_ori = self.decoder.generator.cost_function(images_masked, decoded_het)

        # Reconstruction mask for projections (Train size)
        mask_imgs = self.decoder.generator.resizeImageFourier(self.decoder.generator.mask_imgs, self.train_size)
        mask_imgs = tf.abs(mask_imgs)
        mask_imgs = tf.math.divide_no_nan(mask_imgs, mask_imgs)

        # Reconstruction loss for downscaled images
        images_masked = mask_imgs * self.decoder.generator.resizeImageFourier(images, self.train_size)
        decoded_het_scl = self.decoder.generator.resizeImageFourier(decoded_het, self.train_size)
        loss_het_scl = self.decoder.generator.cost_function(images_masked, decoded_het_scl)

        # MR loss
        if self.filters is not None:
            filt_images = apply_blur_filters_to_batch(images, self.filters)
            filt_decoded = apply_blur_filters_to_batch(decoded_het, self.filters)
            for idx in range(self.multires_levels):
                loss_het_ori += self.decoder.generator.cost_function(filt_images[..., idx][..., None],
                                                                     filt_decoded[..., idx][..., None])
            loss_het_ori = loss_het_ori / (float(self.multires_levels) + 1)

        # Final losses
        rec_loss = loss_het_ori + loss_het_scl
        reg_loss = l1_loss_het + neg_loss_het

        total_loss = 0.5 * rec_loss + 0.5 * reg_loss

        self.total_loss_tracker.update_state(total_loss)
        self.loss_het_tracker.update_state(rec_loss)
        return {
            "loss": self.total_loss_tracker.result(),
            "rec_loss": self.loss_het_tracker.result(),
        }

    def eval_encoder(self, x):
        # Precompute batch aligments
        self.decoder.generator.rot_batch = x[1]
        self.decoder.generator.tilt_batch = x[1]
        self.decoder.generator.psi_batch = x[1]

        # Precompute batch shifts
        self.decoder.generator.shifts_batch = [x[2][:, 0], x[2][:, 1]]

        # Precompute batch CTFs
        self.decoder.generator.ctf = x[3]

        # Wiener filter
        if self.CTF == "wiener":
            x[0] = self.decoder.generator.wiener2DFilter(x[0])

        rot, shift, het = self.encoder.forward(x[0])

        return self.refPose * rot.numpy(), self.refPose * shift.numpy(), het.numpy()

    def eval_volume_het(self, x_het, allCoords=False, filter=True, only_pos=False, add_to_original=False):
        batch_size = x_het.shape[0]

        if allCoords and self.decoder.generator.step > 1:
            new_coords, prev_coords = self.decoder.generator.getAllCoordsMask(), \
                                      self.decoder.generator.coords
        else:
            new_coords = [self.decoder.generator.coords]

        # Read original volume (if needed)
        volume_path = Path(self.decoder.generator.filename.parent, 'volume.mrc')
        if add_to_original and volume_path.exists():
            original_volume = ImageHandler(str(volume_path)).getData()
            original_volume = np.tile(original_volume[None, ...], (x_het.shape[0], 1, 1, 1))
        else:
            original_volume = None

        # Volume
        volume = np.zeros((batch_size, self.decoder.generator.xsize,
                           self.decoder.generator.xsize,
                           self.decoder.generator.xsize), dtype=np.float32)
        for coords in new_coords:
            self.decoder.generator.coords = coords
            volume += self.decoder.eval_volume_het(x_het, filter=filter, only_pos=only_pos)

        # if original_volume is not None:
        #     original_norm = match_histograms(original_volume, volume)
        #     # original_norm = normalize_to_other_volumes(volume, original_volume)
        #     volume = original_norm + volume

        if allCoords and self.decoder.generator.step > 1:
            self.decoder.generator.coords = prev_coords

        return volume

    def predict(self, data, predict_mode="het", applyCTF=False):
        self.predict_mode, self.applyCTF = predict_mode, applyCTF
        self.predict_function = None
        decoded = super().predict(data)
        return decoded

    def predict_step(self, data):
        inputs = data[0]

        if self.mode == "spa":
            indexes = data[1]
            images = inputs
        elif self.mode == "tomo":
            indexes = data[1][0]
            images = inputs[0]

        self.decoder.generator.indexes = indexes
        self.decoder.generator.current_images = images

        # Update batch_size (in case it is incomplete)
        batch_size_scope = tf.shape(images)[0]

        # Precompute batch aligments
        self.decoder.generator.rot_batch = tf.gather(self.decoder.generator.angle_rot, indexes, axis=0)
        self.decoder.generator.tilt_batch = tf.gather(self.decoder.generator.angle_tilt, indexes, axis=0)
        self.decoder.generator.psi_batch = tf.gather(self.decoder.generator.angle_psi, indexes, axis=0)

        # Precompute batch shifts
        self.decoder.generator.shifts_batch = [tf.gather(self.decoder.generator.shift_x, indexes, axis=0),
                                               tf.gather(self.decoder.generator.shift_y, indexes, axis=0)]

        # Precompute batch CTFs
        defocusU_batch = tf.gather(self.decoder.generator.defocusU, indexes, axis=0)
        defocusV_batch = tf.gather(self.decoder.generator.defocusV, indexes, axis=0)
        defocusAngle_batch = tf.gather(self.decoder.generator.defocusAngle, indexes, axis=0)
        cs_batch = tf.gather(self.decoder.generator.cs, indexes, axis=0)
        kv_batch = self.decoder.generator.kv
        ctf = computeCTF(defocusU_batch, defocusV_batch, defocusAngle_batch, cs_batch, kv_batch,
                         self.decoder.generator.sr, self.decoder.generator.pad_factor,
                         [self.decoder.generator.xsize, int(0.5 * self.decoder.generator.xsize + 1)],
                         batch_size_scope, self.decoder.generator.applyCTF)
        self.decoder.generator.ctf = ctf

        # Wiener filter
        if self.CTF == "wiener":
            images = self.decoder.generator.wiener2DFilter(images)
            if self.mode == "spa":
                inputs = images
            elif self.mode == "tomo":
                inputs[0] = images

        # Predict images with CTF applied?
        if self.applyCTF == 1:
            self.decoder.generator.CTF = "apply"
        else:
            self.decoder.generator.CTF = None

        if self.predict_mode == "het":
            return self.encoder(inputs)
        elif self.predict_mode == "particles":
            return self.decoder(self.encoder(inputs))
        else:
            raise ValueError("Prediction mode not understood!")

    def call(self, input_features):
        decoded = self.decoder(self.encoder(input_features))
        return decoded
