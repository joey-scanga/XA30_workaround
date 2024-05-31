#!/usr/bin/env python3
import sys
import json
import shutil
import re
from pathlib import Path
import argparse
import numpy as np
import nibabel as nib
from xa30_workaround.dicom import dicom2nifti
from xa30_workaround.dat import dat_to_array


def normalize(data):
    """Normalize the data to be between 0 and 1."""
    return (data - np.min(data)) / (np.max(data) - np.min(data))


def main():
    parser = argparse.ArgumentParser(description="Convert DICOM and .dat to NIFTI", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")

    # parse arguments
    args, other_args = parser.parse_known_args()

    # get help
    if args.help:
        print("Modified version of dcm2niix that can convert .dat files to NIFTI.")
        print("You should put the .dat files next to the associated DICOM files.")
        print("Below is the original dcm2niix help:\n")
        dicom2nifti("-h")
        sys.exit(0)

    # we need verbose output to get the dicom location
    if "-v" not in other_args:
        idx = len(other_args) - 1
        other_args.insert(idx, "1")
        other_args.insert(idx, "-v")
    else:
        raise ValueError("Turn off verbose output (-v) as this conflicts with this script.")

    # dicom dir
    if "-o" in other_args:
        # get the index of the -o argument
        o_index = other_args.index("-o")
        # get the output directory
        output_dir = Path(other_args[o_index + 1])
        output_dir.mkdir(parents=True, exist_ok=True)

    # run dcm2niix
    dicoms_nii_map = dicom2nifti(*other_args)

    # loop over each nifti file
    for nifti in dicoms_nii_map:
        # get the associated dicom file
        dicom = dicoms_nii_map[nifti]

        # search for alTE tag in dicom file header (this is not a DICOM tag so we need to search by text)
        TEs = None
        with open(dicom, "rb") as f:
            # search for alTE and get the 8 next lines after
            lines = f.readlines()
            for i, line in enumerate(lines):
                try:
                    line = line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if "alTE" in line:
                    TEs = np.array(
                        [float(l.decode("utf-8").strip().split("= \t")[1]) / 1e6 for l in lines[i + 1 : i + 9]]
                    )
                    # only grab valid TEs
                    diff = TEs[1:] - TEs[0:-1]
                    TEs = np.insert(TEs[1:][diff > 0], 0, TEs[0])
                    break

        # load the json file of the nifti
        nifti_json = Path(nifti).with_suffix(".json")
        if not nifti_json.exists():
            raise ValueError(f"Could not find json file {nifti_json}.")
        with open(nifti_json, "r") as f:
            metadata = json.load(f)

        # load the nifti file
        nifti_img_path = Path(nifti).with_suffix(".nii")
        suffix = ".nii"
        if not nifti_img_path.exists():
            nifti_img_path = Path(nifti).with_suffix(".nii.gz")
            suffix = ".nii.gz"
            if not nifti_img_path.exists():
                raise ValueError(f"Could not find nifti file {nifti_img_path}.")
        nifti_img = nib.load(nifti_img_path)

        # get the shape of the nifti file
        shape = nifti_img.shape
        if len(shape) == 3:
            shape = tuple([*shape, 1])
        rshape = list(shape[::-1])
        rshape[0] = TEs.shape[0]  # we want # of TEs instead of frames

        # now search for .dat files that neighbor the exemplar dicom file
        dat_files = list(dicom.parent.glob("*.dat"))

        # if no .dat files were found, then skip this nifti
        if len(dat_files) == 0:
            print(f"Could not find any .dat files associated with {dicom}.")
            continue

        # convert these files to a numpy array
        print(f"Found {len(dat_files)} .dat files associated with {dicom}.")
        print("Converting .dat files to nifti...")
        data_array = dat_to_array(dat_files, rshape)

        # check if number of frames in nifti matches number of frames in .dat files
        if data_array.shape[-1] != shape[-1]:
            raise ValueError("The number of frames in the .dat files does not match the number of frames in the nifti.")

        # do first echo, first frame sanity check
        if len(nifti_img.dataobj.shape) == 3:
            if not np.all(np.isclose(normalize(data_array[..., 0, 0].astype("f8")), normalize(nifti_img.dataobj[...]))):
                raise ValueError(
                    "Sanity check failed. The first echo, first frame of the .dat files does not match the nifti."
                )
        else:
            if not np.all(np.isclose(normalize(data_array[..., 0, 0].astype("f8")), normalize(nifti_img.dataobj[..., 0]))):
                raise ValueError(
                    "Sanity check failed. The first echo, first frame of the .dat files does not match the nifti."
                )

        # loop over each echo skipping the first one
        # only renaming if neccessary
        print("Saving nifti files...")
        nifti = Path(nifti)
        echo_prefix = "e"
        for i, t in enumerate(TEs):
            if i == 0:
                # check if e1 is in the filename
                if "e1" not in nifti.name:
                    # skip if echo1 is in filename
                    if "echo1" in nifti.name:
                        echo_prefix = "echo"
                        continue
                    # if not, then add it to the nifti name and rename the file
                    orig_img_path = nifti_img_path
                    orig_json_path = nifti_json
                    if "_ph" in nifti.name:
                        nifti = Path(str(nifti).replace("_ph", "_e1_ph"))
                    else:
                        nifti = Path(str(nifti) + "_e1")
                    nifti_img_path = nifti.with_suffix(suffix)
                    nifti_json = nifti.with_suffix(".json")
                    shutil.move(orig_img_path, nifti_img_path)
                    shutil.move(orig_json_path, nifti_json)
                # TODO: remove later if fixed
                # resave the phase image with the dat data
                if "_ph" in nifti.name:
                    nib.Nifti1Image(data_array[..., 0, :], nifti_img.affine, nifti_img.header).to_filename(
                        nifti_img_path
                    )
                continue
            # substitute the echo in output_filename
            output_base = Path(str(nifti).replace(f"{echo_prefix}1", f"{echo_prefix}{i + 1}"))
            # copy the metadata
            metadata_copy = metadata.copy()
            # replace the echo time
            metadata_copy["EchoTime"] = t
            # replace the ConversionSoftware
            metadata_copy["ConversionSoftware"] = "dcmdat2niix"
            # set the proper TE type in ImageTypeText
            try:
                te_idx = [t for t in range(len(metadata_copy["ImageTypeText"])) if 'TE' in metadata_copy["ImageTypeText"][t]][0]
                metadata_copy["ImageTypeText"][te_idx] = f"TE{str(i + 1)}"
            except IndexError:
                pass
            # save the nifti file
            output_path = output_base.with_suffix(suffix)
            nib.Nifti1Image(data_array[..., i, :], nifti_img.affine, nifti_img.header).to_filename(output_path)
            # save the json file
            output_json = output_path.with_suffix(".json")
            with open(output_json, "w") as f:
                json.dump(metadata_copy, f, indent=4)
    print("Done.")


if __name__ == "__main__":
    main()
