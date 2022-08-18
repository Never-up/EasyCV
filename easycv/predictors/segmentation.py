# Copyright (c) Alibaba, Inc. and its affiliates.
import cv2
import mmcv
import numpy as np
import torch
from matplotlib.collections import PatchCollection
from matplotlib.patches import Polygon
from torchvision.transforms import Compose

from easycv.core.visualization.image import imshow_bboxes
from easycv.datasets.registry import PIPELINES
from easycv.file import io
from easycv.models import build_model
from easycv.predictors.builder import PREDICTORS
from easycv.predictors.interface import PredictorInterface
from easycv.utils.checkpoint import load_checkpoint
from easycv.utils.config_tools import mmcv_config_fromfile
from easycv.utils.registry import build_from_cfg


@PREDICTORS.register_module()
class Mask2formerPredictor(PredictorInterface):

    def __init__(self, model_path, model_config=None):
        """init model

        Args:
            model_path (str): Path of model path
            model_config (config, optional): config string for model to init. Defaults to None.
        """
        self.model_path = model_path

        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model = None
        with io.open(self.model_path, 'rb') as infile:
            checkpoint = torch.load(infile, map_location='cpu')

        assert 'meta' in checkpoint and 'config' in checkpoint[
            'meta'], 'meta.config is missing from checkpoint'

        self.cfg = checkpoint['meta']['config']
        self.classes = len(self.cfg.PALETTE)
        self.class_name = self.cfg.CLASSES
        # build model
        self.model = build_model(self.cfg.model)

        self.ckpt = load_checkpoint(
            self.model, self.model_path, map_location=self.device)
        self.model.to(self.device)
        self.model.eval()

        # build pipeline
        test_pipeline = self.cfg.test_pipeline
        pipeline = [build_from_cfg(p, PIPELINES) for p in test_pipeline]
        self.pipeline = Compose(pipeline)

    def predict(self, input_data_list, mode='panoptic'):
        """
        Args:
            input_data_list: a list of numpy array(in rgb order), each array is a sample
        to be predicted
        """
        output_list = []
        for idx, img in enumerate(input_data_list):
            output = {}
            if not isinstance(img, np.ndarray):
                img = np.asarray(img)
            data_dict = {'img': img}
            ori_shape = img.shape
            data_dict = self.pipeline(data_dict)
            img = data_dict['img']
            img[0] = torch.unsqueeze(img[0], 0).to(self.device)
            img_metas = [[
                img_meta._data for img_meta in data_dict['img_metas']
            ]]
            img_metas[0][0]['ori_shape'] = ori_shape
            res = self.model.forward_test(img, img_metas, encode=False)
            if mode == 'panoptic':
                output['pan'] = res['pan_results'][0]
            elif mode == 'instance':
                output['segms'] = res['detection_masks'][0]
                output['bboxes'] = res['detection_boxes'][0]
                output['scores'] = res['detection_scores'][0]
                output['labels'] = res['detection_classes'][0]
            output_list.append(output)
        return output_list

    def show_panoptic(self, img, pan_mask):
        pan_label = np.unique(pan_mask)
        pan_label = pan_label[pan_label % 1000 != self.classes]
        masks = np.array([pan_mask == num for num in pan_label])

        palette = np.asarray(self.cfg.PALETTE)
        palette = palette[pan_label % 1000]
        panoptic_result = draw_masks(img, masks, palette)
        return panoptic_result

    def show_instance(self, img, segms, bboxes, scores, labels, score_thr=0.5):
        if score_thr > 0:
            inds = scores > score_thr
            bboxes = bboxes[inds, :]
            segms = segms[inds, ...]
            labels = labels[inds]
        palette = np.asarray(self.cfg.PALETTE)
        palette = palette[labels]
        instance_result = draw_masks(img, segms, palette)
        class_name = np.array(self.class_name)
        instance_result = imshow_bboxes(
            instance_result, bboxes, class_name[labels], show=False)
        return instance_result


@PREDICTORS.register_module()
class SegFormerPredictor(PredictorInterface):

    def __init__(self, model_path, model_config):
        """init model

        Args:
            model_path (str): Path of model path
            model_config (config): config string for model to init. Defaults to None.
        """
        self.model_path = model_path

        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model = None
        with io.open(self.model_path, 'rb') as infile:
            checkpoint = torch.load(infile, map_location='cpu')

        self.cfg = mmcv_config_fromfile(model_config)
        self.CLASSES = self.cfg.CLASSES
        self.PALETTE = self.cfg.PALETTE
        # build model
        self.model = build_model(self.cfg.model)

        self.ckpt = load_checkpoint(
            self.model, self.model_path, map_location=self.device)
        self.model.to(self.device)
        self.model.eval()

        # build pipeline
        test_pipeline = self.cfg.test_pipeline
        pipeline = [build_from_cfg(p, PIPELINES) for p in test_pipeline]
        self.pipeline = Compose(pipeline)

    def predict(self, input_data_list):
        """
    using session run predict a number of samples using batch_size

    Args:
      input_data_list:  a list of numpy array(in rgb order), each array is a sample
        to be predicted
        use a fixed number if you do not want to adjust batch_size in runtime
    """
        output_list = []
        for idx, img in enumerate(input_data_list):
            if type(img) is not np.ndarray:
                img = np.asarray(img)

            ori_img_shape = img.shape[:2]

            data_dict = {'img': img}
            data_dict['ori_shape'] = ori_img_shape
            data_dict = self.pipeline(data_dict)
            img = data_dict['img']
            img = torch.unsqueeze(img[0], 0).to(self.device)
            data_dict.pop('img')

            with torch.no_grad():
                out = self.model([img],
                                 mode='test',
                                 img_metas=[[data_dict['img_metas'][0]._data]])

            output_list.append(out)

        return output_list

    def show_result(self,
                    img,
                    result,
                    palette=None,
                    win_name='',
                    show=False,
                    wait_time=0,
                    out_file=None,
                    opacity=0.5):
        """Draw `result` over `img`.

        Args:
            img (str or Tensor): The image to be displayed.
            result (Tensor): The semantic segmentation results to draw over
                `img`.
            palette (list[list[int]]] | np.ndarray | None): The palette of
                segmentation map. If None is given, random palette will be
                generated. Default: None
            win_name (str): The window name.
            wait_time (int): Value of waitKey param.
                Default: 0.
            show (bool): Whether to show the image.
                Default: False.
            out_file (str or None): The filename to write the image.
                Default: None.
            opacity(float): Opacity of painted segmentation map.
                Default 0.5.
                Must be in (0, 1] range.
        Returns:
            img (Tensor): Only if not `show` or `out_file`
        """

        img = mmcv.imread(img)
        img = img.copy()
        seg = result[0]
        if palette is None:
            if self.PALETTE is None:
                # Get random state before set seed,
                # and restore random state later.
                # It will prevent loss of randomness, as the palette
                # may be different in each iteration if not specified.
                # See: https://github.com/open-mmlab/mmdetection/issues/5844
                state = np.random.get_state()
                np.random.seed(42)
                # random palette
                palette = np.random.randint(
                    0, 255, size=(len(self.CLASSES), 3))
                np.random.set_state(state)
            else:
                palette = self.PALETTE
        palette = np.array(palette)
        assert palette.shape[0] == len(self.CLASSES)
        assert palette.shape[1] == 3
        assert len(palette.shape) == 2
        assert 0 < opacity <= 1.0
        color_seg = np.zeros((seg.shape[0], seg.shape[1], 3), dtype=np.uint8)
        for label, color in enumerate(palette):
            color_seg[seg == label, :] = color
        # convert to BGR
        color_seg = color_seg[..., ::-1]

        img = img * (1 - opacity) + color_seg * opacity
        img = img.astype(np.uint8)
        # if out_file specified, do not show image in window
        if out_file is not None:
            show = False

        if show:
            mmcv.imshow(img, win_name, wait_time)
        if out_file is not None:
            mmcv.imwrite(img, out_file)

        if not (show or out_file):
            return img


def _get_bias_color(base, max_dist=30):
    """Get different colors for each masks.

    Get different colors for each masks by adding a bias
    color to the base category color.
    Args:
        base (ndarray): The base category color with the shape
            of (3, ).
        max_dist (int): The max distance of bias. Default: 30.

    Returns:
        ndarray: The new color for a mask with the shape of (3, ).
    """
    new_color = base + np.random.randint(
        low=-max_dist, high=max_dist + 1, size=3)
    return np.clip(new_color, 0, 255, new_color)


def draw_masks(img, masks, color=None, with_edge=True, alpha=0.8):
    """Draw masks on the image and their edges on the axes.

    Args:
        ax (matplotlib.Axes): The input axes.
        img (ndarray): The image with the shape of (3, h, w).
        masks (ndarray): The masks with the shape of (n, h, w).
        color (ndarray): The colors for each masks with the shape
            of (n, 3).
        with_edge (bool): Whether to draw edges. Default: True.
        alpha (float): Transparency of bounding boxes. Default: 0.8.

    Returns:
        matplotlib.Axes: The result axes.
        ndarray: The result image.
    """
    taken_colors = set([0, 0, 0])
    if color is None:
        random_colors = np.random.randint(0, 255, (masks.size(0), 3))
        color = [tuple(c) for c in random_colors]
        color = np.array(color, dtype=np.uint8)
    polygons = []
    for i, mask in enumerate(masks):
        if with_edge:
            contours, _ = bitmap_to_polygon(mask)
            polygons += [Polygon(c) for c in contours]

        color_mask = color[i]
        while tuple(color_mask) in taken_colors:
            color_mask = _get_bias_color(color_mask)
        taken_colors.add(tuple(color_mask))

        mask = mask.astype(bool)
        img[mask] = img[mask] * (1 - alpha) + color_mask * alpha

    p = PatchCollection(
        polygons, facecolor='none', edgecolors='w', linewidths=1, alpha=0.8)

    return img


def bitmap_to_polygon(bitmap):
    """Convert masks from the form of bitmaps to polygons.

    Args:
        bitmap (ndarray): masks in bitmap representation.

    Return:
        list[ndarray]: the converted mask in polygon representation.
        bool: whether the mask has holes.
    """
    bitmap = np.ascontiguousarray(bitmap).astype(np.uint8)
    # cv2.RETR_CCOMP: retrieves all of the contours and organizes them
    #   into a two-level hierarchy. At the top level, there are external
    #   boundaries of the components. At the second level, there are
    #   boundaries of the holes. If there is another contour inside a hole
    #   of a connected component, it is still put at the top level.
    # cv2.CHAIN_APPROX_NONE: stores absolutely all the contour points.
    outs = cv2.findContours(bitmap, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    contours = outs[-2]
    hierarchy = outs[-1]
    if hierarchy is None:
        return [], False
    # hierarchy[i]: 4 elements, for the indexes of next, previous,
    # parent, or nested contours. If there is no corresponding contour,
    # it will be -1.
    with_hole = (hierarchy.reshape(-1, 4)[:, 3] >= 0).any()
    contours = [c.reshape(-1, 2) for c in contours]
    return contours, with_hole
