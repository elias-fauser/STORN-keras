import keras
import keras.backend as K
import greenarm.models.keras_fix as kf

# Apply the keras fix

import keras.layers as l

def compute_mask_sum_mul_ave(self, inputs, mask=None):
    if mask is None or all([m is None for m in mask]):
        return None

    assert hasattr(mask, '__len__') and len(mask) == len(inputs)

    masks = [K.expand_dims(m, 0) for m in mask if m is not None]
    return K.all(K.concatenate(masks, axis=0), axis=0, keepdims=False)

def compute_mask_concat(self, inputs, mask=None):
    if mask is None or all([m is None for m in mask]):
        return None

    assert hasattr(mask, '__len__') and len(mask) == len(inputs)

    # Make a list of masks while making sure the dimensionality of each mask
    # is the same as the corresponding input.
    masks = []
    for input_i, mask_i in zip(inputs, mask):
        if mask_i is None:
            # Input is unmasked. Append all 1s to masks
            masks.append(K.ones_like(input_i))
        elif K.ndim(mask_i) < K.ndim(input_i):
            # Mask is smaller than the input, expand it
            masks.append(K.expand_dims(mask_i))
        else:
            masks.append(mask)
    concatenated = K.concatenate(masks, axis=self.concat_axis)
    return K.all(concatenated, axis=-1, keepdims=False)

# Mokey Patch
# print("Applying keras fix...")
# l.Concatenate.compute_mask = compute_mask_concat
# l.Add.compute_mask = compute_mask_sum_mul_ave
# l.Subtract.compute_mask = compute_mask_sum_mul_ave
# l.Multiply.compute_mask = compute_mask_sum_mul_ave
# l.Average.compute_mask = compute_mask_sum_mul_ave