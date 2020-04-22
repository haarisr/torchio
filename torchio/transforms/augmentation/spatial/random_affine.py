from numbers import Number
from typing import Tuple, Optional, List, Union
import torch
import numpy as np
import SimpleITK as sitk
from ....data.images import Subject
from ....utils import is_image_dict
from ....torchio import LABEL, DATA, AFFINE, TYPE, TypeRangeFloat
from .. import Interpolation, get_sitk_interpolator
from .. import RandomTransform


class RandomAffine(RandomTransform):
    r"""Random affine transformation.

    Args:
        scales: Tuple :math:`(a, b)` defining the scaling
            magnitude. The scaling values along each dimension are
            :math:`(s_1, s_2, s_3)`, where :math:`s_i \sim \mathcal{U}(a, b)`.
            For example, using ``scales=(0.5, 0.5)`` will zoom out the image,
            making the objects inside look twice as small while preserving
            the physical size and position of the image.
        degrees: Tuple :math:`(a, b)` defining the rotation range in degrees.
            The rotation angles around each axis are
            :math:`(\theta_1, \theta_2, \theta_3)`,
            where :math:`\theta_i \sim \mathcal{U}(a, b)`.
            If only one value :math:`d` is provided,
            :math:`\theta_i \sim \mathcal{U}(-d, d)`.
        isotropic: If ``True``, the scaling factor along all dimensions is the
            same, i.e. :math:`s_1 = s_2 = s_3`.
        default_pad_value: As the image is rotated, some values near the
            borders will be undefined.
            If ``'minimum'``, the fill value will be the image minimum.
            If ``'mean'``, the fill value is the mean of the border values.
            If ``'otsu'``, the fill value is the mean of the values at the
            border that lie under an
            `Otsu threshold <https://ieeexplore.ieee.org/document/4310076>`_.
        image_interpolation: See :ref:`Interpolation`.
        p: Probability that this transform will be applied.
        seed: See :py:class:`~torchio.transforms.augmentation.RandomTransform`.

    .. note:: Rotations are performed around the center of the image.

    Example:
        >>> from torchio.transforms import RandomAffine, Interpolation
        >>> sample = images_dataset[0]  # instance of torchio.ImagesDataset
        >>> transform = RandomAffine(
        ...     scales=(0.9, 1.2),
        ...     degrees=(10),
        ...     isotropic=False,
        ...     default_pad_value='otsu',
        ...     image_interpolation=Interpolation.BSPLINE,
        ... )
        >>> transformed = transform(sample)

    From the command line::

        $ torchio-transform t1.nii.gz RandomAffine -k "degrees=30 default_pad_value=minimum" -s 42 affine_min.nii.gz

    """
    def __init__(
            self,
            scales: Tuple[float, float] = (0.9, 1.1),
            degrees: TypeRangeFloat = 10,
            isotropic: bool = False,
            default_pad_value: Union[str, float] = 'otsu',
            image_interpolation: Interpolation = Interpolation.LINEAR,
            p: float = 1,
            seed: Optional[int] = None,
            ):
        super().__init__(p=p, seed=seed)
        self.scales = scales
        self.degrees = self.parse_degrees(degrees)
        self.isotropic = isotropic
        self.default_pad_value = self.parse_default_value(default_pad_value)
        self.interpolation = self.parse_interpolation(image_interpolation)

    @staticmethod
    def parse_default_value(value: Union[str, float]) -> Union[str, float]:
        if isinstance(value, Number) or value in ('minimum', 'otsu', 'mean'):
            return value
        message = (
            'Value for default_pad_value must be "minimum", "otsu", "mean"'
            ' or a number'
        )
        raise ValueError(message)

    def apply_transform(self, sample: Subject) -> dict:
        sample.check_consistent_shape()
        scaling_params, rotation_params = self.get_params(
            self.scales, self.degrees, self.isotropic)
        sample['random_scaling'] = scaling_params
        sample['random_rotation'] = rotation_params
        for image_dict in sample.values():
            if not is_image_dict(image_dict):
                continue
            if image_dict[TYPE] == LABEL:
                interpolation = Interpolation.NEAREST
            else:
                interpolation = self.interpolation
            image_dict[DATA] = self.apply_affine_transform(
                image_dict[DATA],
                image_dict[AFFINE],
                scaling_params,
                rotation_params,
                interpolation,
            )
        return sample

    @staticmethod
    def get_params(
            scales: Tuple[float, float],
            degrees: Tuple[float, float],
            isotropic: bool,
            ) -> Tuple[List[float], List[float]]:
        scaling_params = torch.FloatTensor(3).uniform_(*scales)
        if isotropic:
            scaling_params.fill_(scaling_params[0])
        rotation_params = torch.FloatTensor(3).uniform_(*degrees)
        return scaling_params.tolist(), rotation_params.tolist()

    @staticmethod
    def get_scaling_transform(
            scaling_params: List[float],
            ) -> sitk.ScaleTransform:
        # scaling_params are inverted so that they are more intuitive
        # For example, 1.5 means the objects look 1.5 times larger
        transform = sitk.ScaleTransform(3)
        scaling_params = 1 / np.array(scaling_params)
        transform.SetScale(scaling_params)
        return transform

    @staticmethod
    def get_rotation_transform(
            degrees: List[float],
            ) -> sitk.Euler3DTransform:
        transform = sitk.Euler3DTransform()
        radians = np.radians(degrees)
        transform.SetRotation(*radians)
        return transform

    def apply_affine_transform(
            self,
            tensor: torch.Tensor,
            affine: np.ndarray,
            scaling_params: List[float],
            rotation_params: List[float],
            interpolation: Interpolation,
            ) -> torch.Tensor:
        assert tensor.ndim == 4
        assert len(tensor) == 1

        image = self.nib_to_sitk(tensor[0], affine)
        floating = reference = image

        scaling_transform = self.get_scaling_transform(scaling_params)
        rotation_transform = self.get_rotation_transform(rotation_params)
        transform = sitk.Transform(3, sitk.sitkComposite)
        transform.AddTransform(scaling_transform)
        transform.AddTransform(rotation_transform)

        if self.default_pad_value == 'minimum':
            default_value = tensor.min().item()
        elif self.default_pad_value == 'mean':
            default_value = get_borders_mean(image, filter_otsu=False)
        elif self.default_pad_value == 'otsu':
            default_value = get_borders_mean(image, filter_otsu=True)
        else:
            default_value = self.default_pad_value

        resampler = sitk.ResampleImageFilter()
        resampler.SetInterpolator(get_sitk_interpolator(interpolation))
        resampler.SetReferenceImage(reference)
        resampler.SetDefaultPixelValue(float(default_value))
        resampler.SetOutputPixelType(sitk.sitkFloat32)
        resampler.SetTransform(transform)
        resampled = resampler.Execute(floating)

        np_array = sitk.GetArrayFromImage(resampled)
        np_array = np_array.transpose()  # ITK to NumPy
        tensor[0] = torch.from_numpy(np_array)
        return tensor


def get_borders_mean(image, filter_otsu=True):
    array = sitk.GetArrayViewFromImage(image)
    borders = np.array((
        array[0],
        array[-1],
        array[0, :, :],
        array[-1, :, :],
        array[:, 0, :],
        array[:, -1, :],
        array[:, :, 0],
        array[:, :, -1],
    ))
    borders = np.hstack([border.flatten() for border in borders])
    if not filter_otsu:
        return borders.mean()
    borders = borders.reshape(1, 1, -1)
    borders_image = sitk.GetImageFromArray(borders)
    otsu = sitk.OtsuThresholdImageFilter()
    otsu.Execute(borders_image)
    threshold = otsu.GetThreshold()
    values = borders[borders < threshold]
    if values.any():
        default_value = values.mean()
    else:
        default_value = borders.mean()
    return default_value
