# STORN implementation for keras

The implementation can be found in greenarm/models/STORN.py!

The original implementation was updated to work with the newest Keras and Theano/ Tensorflow version

### Required packages
 - Python 2.7 / 3.7
 - keras >= 2.2
 - theano >= 1.0.4
 - scikit-learn>=0.2
 - h5py = 2.6.0
 - hdf5 = 1.8.16
 - tensorflow
 - [hualos](https://github.com/fchollet/hualos) (optional monitoring)

The dependencies might be installed with the `pip` requirements file.

### Changes
 - 'LambdaWithMasking' class conforms to the new Keras Object Model
 - Functional API calls to `merge` were replaced by `Concatenate`
 - Name Scopes were introduced
 - Hard coded data dimensionality was moved into keyword arguments
 - Shape function calls were updated

---
**NOTE**

The Keras Fix required by the origin implementation of @Durner is not required anymore but can be enabled by uncommenting the code in the `greenarm.__init__.py` 

---
